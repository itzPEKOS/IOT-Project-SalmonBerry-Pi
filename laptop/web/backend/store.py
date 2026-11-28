"""ชื่อผู้เล่นและผลแต่ละรอบใน SQLite (ค่าจาก sensor ตามเวลายังอยู่ใน InfluxDB)

อันดับ เวลาดีที่สุด และ new record ไม่เก็บแยก คำนวณตอนเรียกดู
ใช้จาก thread เดียว (event loop ของ backend)
"""
import math
import pathlib
import re
import sqlite3
import time

NAME = re.compile(r"[A-Z0-9_-]{1,10}")
ROUND_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESULTS = {"WIN", "GAME_OVER", "ABORTED"}
MODES = ("rlgl", "sniper")
DEFAULT_MODE = "rlgl"
FINISHED = "result IN ('WIN', 'GAME_OVER')"
TEXT_MAX = 32

# field ที่รับจากข้อความ MQTT และชนิดที่ยอมรับ ค่าที่ผิดชนิดถูกเก็บเป็นว่าง
RESULT_FIELDS = {"reason": str, "duration_s": float, "caught_part": str, "caught_red_s": float,
                 "caught_score_ratio": float, "bpm_rest": float, "hits": float, "dodges": float}
# คอลัมน์ที่เพิ่มทีหลัง: ฐานข้อมูลเก่าจะถูกเพิ่มให้ตอนเปิด
ADDED_COLUMNS = {"mode": f"TEXT NOT NULL DEFAULT '{DEFAULT_MODE}'", "hits": "INTEGER", "dodges": "INTEGER"}
SUMMARY_FIELDS = {"bpm_avg": float, "bpm_max": float, "red_light_count": float, "red_motion_max": float,
                  "red_acc_peak_max": float, "closest_call": float, "distance_cm": float,
                  "finish_state": str}

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    created_at       INTEGER NOT NULL,
    selected_order   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id           TEXT    NOT NULL UNIQUE,
    player_id          INTEGER REFERENCES players(id),
    started_at         INTEGER NOT NULL,
    ended_at           INTEGER,
    duration_s         REAL,
    result             TEXT,
    reason             TEXT,
    finish_state       TEXT,
    caught_part        TEXT,
    caught_red_s       REAL,
    caught_score_ratio REAL,
    distance_cm        REAL,
    bpm_rest           REAL,
    bpm_avg            REAL,
    bpm_max            REAL,
    red_light_count    INTEGER,
    red_motion_max     REAL,
    red_acc_peak_max   REAL,
    closest_call       REAL,
    clip_file          TEXT
);
"""

SELECT_ROUND = """
SELECT r.id AS number, r.round_id, r.mode, p.name AS player, r.started_at, r.ended_at, r.duration_s,
       r.result, r.reason, r.finish_state, r.caught_part, r.caught_red_s, r.caught_score_ratio,
       r.distance_cm, r.bpm_rest, r.bpm_avg, r.bpm_max, r.red_light_count, r.red_motion_max,
       r.red_acc_peak_max, r.closest_call, r.hits, r.dodges, r.clip_file
FROM rounds r LEFT JOIN players p ON p.id = r.player_id
"""


def clean_mode(raw):
    """คืนชื่อโหมดที่ใช้ได้ หรือ None"""
    return raw if raw in MODES else None


def clean_name(raw):
    """คืนชื่อที่ใช้ได้ (ตัวพิมพ์ใหญ่) หรือ None"""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    # เช็กก่อน upper() เพราะตัวอักษรบางตัวนอก ASCII กลายเป็น ASCII หลังแปลง
    if not name.isascii():
        return None
    name = name.upper()
    return name if NAME.fullmatch(name) else None


def valid_round_id(value):
    return isinstance(value, str) and bool(ROUND_ID.fullmatch(value))


def _typed(value, kind):
    if kind is str:
        return value[:TEXT_MAX] if isinstance(value, str) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


class Store:
    def __init__(self, path=None, clock=time.time):
        """path=None เก็บในหน่วยความจำ (หายเมื่อปิดโปรแกรม)"""
        if path is not None:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if path is not None:
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = NORMAL")
        with self.db:
            self.db.executescript(SCHEMA)
            existing = {row["name"] for row in self.db.execute("PRAGMA table_info(rounds)")}
            for column, definition in ADDED_COLUMNS.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE rounds ADD COLUMN {column} {definition}")

    def close(self):
        self.db.close()

    def _now_ms(self):
        return int(self.clock() * 1000)

    def _one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    # ---------- ผู้เล่น ----------

    def select_player(self, name):
        """สร้างผู้เล่นถ้ายังไม่มี และจำว่าเพิ่งถูกเลือก"""
        name = clean_name(name)
        if name is None:
            raise ValueError("invalid player name")
        with self.db:
            self.db.execute(
                "INSERT INTO players (name, created_at, selected_order) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(selected_order), 0) + 1 FROM players)) "
                "ON CONFLICT(name) DO UPDATE SET selected_order = excluded.selected_order",
                (name, self._now_ms()))
        return self._one("SELECT id, name FROM players WHERE name = ?", name)

    def recent_players(self, limit=4):
        return self._all("SELECT name FROM players ORDER BY selected_order DESC LIMIT ?", limit)

    def player_stats(self, name, mode=DEFAULT_MODE):
        """ตัวเลขของผู้เล่นในโหมดนั้น"""
        player = self._one("SELECT id, name FROM players WHERE name = ?", clean_name(name))
        if player is None:
            return None
        counts = self._one(
            f"SELECT COUNT(*) AS rounds_played, COALESCE(SUM(result = 'WIN'), 0) AS wins "
            f"FROM rounds WHERE player_id = ? AND mode = ? AND {FINISHED}", player["id"], mode)
        entry = next((e for e in self._ranking(mode=mode) if e["player"] == player["name"]), None)
        return {"name": player["name"], **counts,
                "best_s": entry["duration_s"] if entry else None,
                "rank": entry["rank"] if entry else None}

    # ---------- รอบการเล่น ----------

    def start_round(self, round_id, player_id, mode=DEFAULT_MODE):
        """บันทึกรอบใหม่ ถ้ามีรอบนี้แล้วไม่เปลี่ยนอะไร คืน True เมื่อสร้างใหม่"""
        if not valid_round_id(round_id):
            return False
        with self.db:
            # ไม่ใช้ INSERT OR IGNORE: id (เลขรอบ) จะไม่กระโดดเมื่อมีข้อความซ้ำ
            cursor = self.db.execute(
                "INSERT INTO rounds (round_id, player_id, started_at, mode) "
                "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM rounds WHERE round_id = ?)",
                (round_id, player_id, self._now_ms(), clean_mode(mode) or DEFAULT_MODE, round_id))
        return cursor.rowcount == 1

    def next_number(self):
        """เลขรอบที่รอบถัดไปจะได้ (ไม่มีการลบรอบ)"""
        return self._one("SELECT COALESCE(MAX(id), 0) + 1 AS number FROM rounds")["number"]

    def assign_player(self, round_id, player_id):
        """เปลี่ยนผู้เล่นของรอบที่ยังไม่มีผล"""
        with self.db:
            self.db.execute("UPDATE rounds SET player_id = ? WHERE round_id = ? AND result IS NULL",
                            (player_id, round_id))

    def finish_round(self, payload):
        """บันทึกผลจาก rlgl/game/result ผลแรกของรอบเท่านั้นที่ถูกเก็บ"""
        round_id, result = payload.get("round_id"), payload.get("result")
        if not valid_round_id(round_id) or result not in tuple(RESULTS):
            return
        # เผื่อ backend ไม่ได้เห็นตอนเริ่มรอบ
        self.start_round(round_id, None, clean_mode(payload.get("mode")) or DEFAULT_MODE)
        values = {field: _typed(payload.get(field), kind) for field, kind in RESULT_FIELDS.items()}
        values.update(result=result, ended_at=self._now_ms())
        self._update(round_id, values, only_if="result IS NULL")

    def save_summary(self, payload):
        values = {field: _typed(payload.get(field), kind) for field, kind in SUMMARY_FIELDS.items()}
        values = {field: value for field, value in values.items() if value is not None}
        if values and valid_round_id(payload.get("round_id")):
            self._update(payload["round_id"], values)

    def set_clip(self, round_id, filename):
        self._update(round_id, {"clip_file": filename})

    def _update(self, round_id, values, only_if="1"):
        # ชื่อคอลัมน์มาจากค่าคงที่ในไฟล์นี้เท่านั้น ส่วนค่าส่งเป็น parameter
        columns = ", ".join(f"{field} = ?" for field in values)
        with self.db:
            self.db.execute(f"UPDATE rounds SET {columns} WHERE round_id = ? AND {only_if}",
                            (*values.values(), round_id))

    def clip_rounds(self):
        """round_id ที่มีวิดีโอแล้ว ใหม่สุดก่อน"""
        return [row["round_id"] for row in self.db.execute(
            "SELECT round_id FROM rounds WHERE clip_file IS NOT NULL ORDER BY COALESCE(ended_at, started_at) DESC, id DESC")]

    def round(self, round_id):
        return self._one(SELECT_ROUND + " WHERE r.round_id = ?", round_id)

    # ---------- ประวัติ ----------

    def history(self, result="all", page=1, page_size=7, mode=None):
        """mode=None คือทุกโหมด"""
        where = {"all": f"r.{FINISHED}", "win": "r.result = 'WIN'", "loss": "r.result = 'GAME_OVER'"}[result]
        # ค่า mode ส่งเป็น parameter ทุกครั้ง: NULL = ไม่กรอง
        same_mode = "(? IS NULL OR mode = ?)"
        counts = self._one(
            "SELECT COALESCE(SUM(result = 'WIN'), 0) AS wins, "
            "COALESCE(SUM(result = 'GAME_OVER'), 0) AS losses, MAX(ended_at) AS last_ended_at "
            f"FROM rounds WHERE {FINISHED} AND {same_mode}", mode, mode)
        total = {"all": counts["wins"] + counts["losses"], "win": counts["wins"], "loss": counts["losses"]}[result]
        rounds = self._all(SELECT_ROUND + f" WHERE {where} AND (? IS NULL OR r.mode = ?) "
                                          "ORDER BY r.ended_at DESC, r.id DESC LIMIT ? OFFSET ?",
                           mode, mode, page_size, (page - 1) * page_size)
        return {"rounds": rounds, "page": page, "page_size": page_size, "total": total,
                "counts": {"all": counts["wins"] + counts["losses"], "wins": counts["wins"],
                           "losses": counts["losses"]},
                "last_ended_at": counts["last_ended_at"]}

    # ---------- อันดับ ----------

    def _wins(self, mode):
        """รอบที่ชนะของผู้เล่นที่มีชื่อในโหมดนั้น เรียงตามลำดับที่จบ"""
        return self._all(
            "SELECT r.id, r.round_id, p.name AS player, r.duration_s, r.bpm_max, r.ended_at "
            "FROM rounds r JOIN players p ON p.id = r.player_id "
            "WHERE r.result = 'WIN' AND r.duration_s IS NOT NULL AND r.mode = ? ORDER BY r.ended_at, r.id", mode)

    def _ranking(self, wins=None, mode=DEFAULT_MODE):
        """เวลาดีที่สุดคนละหนึ่งแถว เวลาเท่ากันให้คนที่ทำได้ก่อนอยู่บน"""
        best = {}
        for order, win in enumerate(self._wins(mode) if wins is None else wins):
            current = best.get(win["player"])
            if current is None or win["duration_s"] < current[1]["duration_s"]:
                best[win["player"]] = (order, win)
        ordered = sorted(best.values(), key=lambda item: (item[1]["duration_s"], item[0]))
        return [{"rank": rank, "player": win["player"], "duration_s": win["duration_s"],
                 "bpm_max": win["bpm_max"], "round_id": win["round_id"]}
                for rank, (_, win) in enumerate(ordered, start=1)]

    def leaderboard(self, limit=7, mode=DEFAULT_MODE):
        return self._ranking(mode=mode)[:limit]

    def standing(self, round_id):
        """ผลของรอบที่ชนะ เทียบกับรอบที่จบก่อนหน้าในโหมดเดียวกันเท่านั้น คืน None ถ้าแพ้หรือไม่มีชื่อ"""
        row = self._one("SELECT mode FROM rounds WHERE round_id = ?", round_id)
        if row is None:
            return None
        wins = self._wins(row["mode"])
        position = next((i for i, w in enumerate(wins) if w["round_id"] == round_id), None)
        if position is None:
            return None
        this, earlier = wins[position], wins[:position]
        own = [w["duration_s"] for w in earlier if w["player"] == this["player"]]
        record = min(earlier, key=lambda w: w["duration_s"], default=None)
        new_record = record is None or this["duration_s"] < record["duration_s"]
        beaten = None
        if new_record and record is not None and record["player"] != this["player"]:
            beaten = {"player": record["player"], "by_s": round(record["duration_s"] - this["duration_s"], 1)}
        rank = next(e["rank"] for e in self._ranking(wins[:position + 1]) if e["player"] == this["player"])
        return {"rank": rank, "personal_best": not own or this["duration_s"] < min(own),
                "new_record": new_record, "beaten": beaten}
