"""โหมดเกม (rlgl / sniper): เลือกจากหน้าเว็บ, บันทึกรอบแยกโหมด, อันดับแยกโหมด"""
import sqlite3

from backend.app import Settings
from backend.store import Store
from test_api import inject, make
from test_players_api import choose, detail


def state(client, name, round_id="s1", mode="sniper", **extra):
    payload = {"round_id": round_id, "state": name, "remaining_s": 30, **extra}
    if mode is not None:
        payload["mode"] = mode
    inject(client, "rlgl/game/state", payload)


def finish(client, round_id="s1", outcome="WIN", reason="touch", duration=20.0, mode="sniper", **extra):
    inject(client, "rlgl/game/result", {"round_id": round_id, "result": outcome, "reason": reason,
                                         "duration_s": duration, "mode": mode, **extra})


def play(client, round_id, mode, duration, outcome="WIN", reason="touch", **extra):
    playing = "SNIPER" if mode == "sniper" else "GREEN_LIGHT"
    state(client, "CALIBRATE", round_id, mode)
    state(client, playing, round_id, mode)
    finish(client, round_id, outcome, reason, duration, mode, **extra)


# ---------- เลือกโหมด ----------

def test_the_backend_announces_the_default_mode_on_start(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        sent = recorder.of("rlgl/game/mode")
        assert sent and sent[-1][1]["mode"] == "rlgl" and sent[-1][3] is True  # retained
        assert client.get("/api/session").json()["mode"] == "rlgl"


def test_choosing_sniper_mode_is_published_for_the_game_engine(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        body = client.post("/api/mode", json={"mode": "sniper"})
        assert body.status_code == 200 and body.json()["mode"] == "sniper"
        topic, payload, qos, retain = recorder.of("rlgl/game/mode")[-1]
        assert (payload["mode"], payload["device_id"], qos, retain) == ("sniper", "backend-01", 1, True)
        assert client.get("/api/session").json()["mode"] == "sniper"
        assert client.get("/api/state").json()["messages"]["rlgl/game/mode"]["mode"] == "sniper"


def test_an_unknown_mode_is_refused(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        for body in ({"mode": "SNIPER2"}, {"mode": 3}, {}):
            assert client.post("/api/mode", json=body).status_code == 422, body
        assert client.get("/api/session").json()["mode"] == "rlgl"


def test_a_mode_message_coming_back_from_the_broker_is_ignored(tmp_path):
    # backend เป็นคน publish เอง ข้อความที่ broker ส่งกลับต้องไม่ทับค่าที่เลือก
    from backend.app import decode_mqtt
    assert decode_mqtt("rlgl/game/mode", b'{"mode": "sniper"}') is None


# ---------- บันทึกรอบ ----------

def test_a_sniper_round_is_saved_with_mode_hits_and_dodges(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        state(client, "CALIBRATE")
        state(client, "SNIPER", lives=3, hits=0, dodges=0)
        inject(client, "rlgl/wrist/heartrate", {"bpm": 130})
        inject(client, "rlgl/gateway/distance", {"distance_cm": 80})
        finish(client, duration=31.5, hits=1, dodges=7)
        saved = detail(client, "s1").json()
        assert (saved["mode"], saved["player"], saved["result"], saved["hits"], saved["dodges"]) == \
               ("sniper", "BERRY", "WIN", 1, 7)
        assert (saved["bpm_max"], saved["distance_cm"]) == (130, 80)


def test_a_round_without_mode_uses_the_mode_chosen_on_the_web(tmp_path):
    client, _ = make(tmp_path)
    with client:
        state(client, "GREEN_LIGHT", "r1", mode=None)
        client.post("/api/mode", json={"mode": "sniper"})
        state(client, "CALIBRATE", "s1", mode=None)
        assert detail(client, "r1").json()["mode"] == "rlgl"
        assert detail(client, "s1").json()["mode"] == "sniper"


def test_the_mode_sent_by_the_game_engine_wins_over_the_web_choice(tmp_path):
    client, _ = make(tmp_path)
    with client:
        client.post("/api/mode", json={"mode": "sniper"})
        state(client, "GREEN_LIGHT", "r1", mode="rlgl")
        assert detail(client, "r1").json()["mode"] == "rlgl"


def test_bad_hits_and_dodges_are_stored_as_empty(tmp_path):
    client, _ = make(tmp_path)
    with client:
        play(client, "s1", "sniper", 20.0, hits="two", dodges=True)
        saved = detail(client, "s1").json()
        assert (saved["hits"], saved["dodges"]) == (None, None)


# ---------- อันดับและประวัติแยกโหมด ----------

def test_each_mode_has_its_own_leaderboard_and_standing(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        play(client, "r1", "rlgl", 20.0)
        choose(client, "salmon")
        play(client, "s1", "sniper", 25.0)
        rlgl = client.get("/api/leaderboard").json()["entries"]
        sniper = client.get("/api/leaderboard?mode=sniper").json()["entries"]
        assert [(e["player"], e["rank"]) for e in rlgl] == [("BERRY", 1)]
        assert [(e["player"], e["rank"]) for e in sniper] == [("SALMON", 1)]
        assert detail(client, "s1").json()["standing"]["new_record"] is True
        assert client.get("/api/leaderboard?mode=other").status_code == 422


def test_history_shows_the_mode_and_can_filter_by_it(tmp_path):
    client, _ = make(tmp_path)
    with client:
        play(client, "r1", "rlgl", 20.0)
        play(client, "s1", "sniper", 25.0, outcome="GAME_OVER", reason="hits")
        everything = client.get("/api/rounds").json()
        assert [(r["round_id"], r["mode"]) for r in everything["rounds"]] == [("s1", "sniper"), ("r1", "rlgl")]
        only = client.get("/api/rounds?mode=sniper").json()
        assert [r["round_id"] for r in only["rounds"]] == ["s1"]
        assert (only["total"], only["counts"]) == (1, {"all": 1, "wins": 0, "losses": 1})


def test_player_stats_follow_the_requested_mode(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        play(client, "r1", "rlgl", 20.0)
        play(client, "s1", "sniper", 25.0)
        play(client, "s2", "sniper", 30.0, outcome="GAME_OVER", reason="hits")
        rlgl = client.get("/api/players/berry").json()
        sniper = client.get("/api/players/berry?mode=sniper").json()
        assert (rlgl["rounds_played"], rlgl["best_s"]) == (1, 20.0)
        assert (sniper["rounds_played"], sniper["wins"], sniper["best_s"]) == (2, 1, 25.0)
        client.post("/api/mode", json={"mode": "sniper"})
        assert client.get("/api/session").json()["player"]["best_s"] == 25.0


# ---------- ค่าตั้งของหน้าเว็บ ----------

def test_config_gives_the_web_page_the_stream_and_grafana_addresses(tmp_path):
    client, _ = make(tmp_path, stream_url="http://192.168.1.50:8080/stream.mjpg",
                     grafana_url="http://localhost:3000/d/rlgl-main", track_cm=350)
    with client:
        assert client.get("/api/config").json() == {"stream_url": "http://192.168.1.50:8080/stream.mjpg",
                                                    "grafana_url": "http://localhost:3000/d/rlgl-main",
                                                    "track_cm": 350}


def test_config_has_defaults_when_nothing_is_set(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert client.get("/api/config").json() == {"stream_url": None, "grafana_url": None, "track_cm": 300}


def test_settings_read_the_web_config_from_the_environment(monkeypatch):
    monkeypatch.setenv("RLGL_STREAM_URL", "http://pi.local:8080/stream.mjpg")
    monkeypatch.setenv("RLGL_GRAFANA_URL", "http://localhost:3000/d/rlgl-main")
    monkeypatch.setenv("RLGL_TRACK_CM", "280")
    settings = Settings.from_env()
    assert (settings.stream_url, settings.grafana_url, settings.track_cm) == \
           ("http://pi.local:8080/stream.mjpg", "http://localhost:3000/d/rlgl-main", 280)


# ---------- ไฟล์หน้าเว็บ ----------

def test_page_files_are_rechecked_so_updates_reach_the_player_screen(tmp_path):
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (web / "js" / "app.js").write_text("// app", encoding="utf-8")
    client, _ = make(tmp_path, web_dir=web)
    with client:
        for path in ("/", "/js/app.js"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers.get("cache-control") == "no-cache", path
        assert "cache-control" not in client.get("/api/health").headers


# ---------- ฐานข้อมูลเก่า ----------

def test_an_old_database_gets_the_new_columns_and_keeps_its_rounds(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE players (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                              created_at INTEGER NOT NULL, selected_order INTEGER NOT NULL);
        CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL UNIQUE,
            player_id INTEGER REFERENCES players(id), started_at INTEGER NOT NULL, ended_at INTEGER,
            duration_s REAL, result TEXT, reason TEXT, finish_state TEXT, caught_part TEXT, caught_red_s REAL,
            caught_score_ratio REAL, distance_cm REAL, bpm_rest REAL, bpm_avg REAL, bpm_max REAL,
            red_light_count INTEGER, red_motion_max REAL, red_acc_peak_max REAL, closest_call REAL, clip_file TEXT);
        INSERT INTO players VALUES (1, 'BERRY', 0, 1);
        INSERT INTO rounds (round_id, player_id, started_at, ended_at, duration_s, result, reason)
            VALUES ('old1', 1, 0, 10, 22.0, 'WIN', 'touch');
    """)
    old.commit()
    old.close()
    store = Store(path)
    try:
        saved = store.round("old1")
        assert (saved["mode"], saved["hits"], saved["dodges"]) == ("rlgl", None, None)
        assert [e["player"] for e in store.leaderboard()] == ["BERRY"]
    finally:
        store.close()
