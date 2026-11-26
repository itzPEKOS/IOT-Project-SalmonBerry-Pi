"""สถานะล่าสุดของเกม และตัวเลขสรุปของแต่ละรอบ (ไม่ผูกกับ MQTT หรือเว็บ)"""
import math
import time
from dataclasses import dataclass, field

STATE = "rlgl/game/state"
RESULT = "rlgl/game/result"
SUMMARY = "rlgl/game/summary"
MEDIA = "rlgl/game/media"
HEARTRATE = "rlgl/wrist/heartrate"
IMU = "rlgl/wrist/imu"
VISION = "rlgl/gateway/vision"

BPM_MIN, BPM_MAX = 45, 180
PLAYING = {"GREEN_LIGHT", "TRANSITION", "RED_LIGHT"}
# ผู้เล่นเริ่มเดินทันทีที่ไฟเขียว และค่าจากข้อมืออาจมาถึงก่อน state ใหม่ (คนละอุปกรณ์)
# สรุปรอบจึงไม่นับค่าข้อมือที่มาถึงภายในช่วงนี้ก่อนเปลี่ยนจากไฟแดงเป็นไฟเขียว
GREEN_EDGE_MARGIN_S = 0.2


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class Outcome:
    accepted: bool = True
    outgoing: list = field(default_factory=list)


@dataclass
class RoundNumbers:
    bpm: list = field(default_factory=list)
    red_motion_max: float | None = None
    red_acc_peak_max: float | None = None
    red_light_count: int = 0
    red_acc_recent: list = field(default_factory=list)  # (เวลาที่มาถึง, ค่า) ที่ยังตัดสินไม่ได้ว่าอยู่รอยต่อไหม


def _highest(current, value):
    if not is_number(value):
        return current
    return value if current is None else max(current, value)


class GameTracker:
    def __init__(self, clock=time.monotonic, green_edge_margin_s=GREEN_EDGE_MARGIN_S):
        self.clock = clock
        self.margin = green_edge_margin_s
        self.latest = {}
        self.round_id = None
        self.state = None
        self.numbers = RoundNumbers()
        self.summarised = set()

    def snapshot(self):
        """ข้อความล่าสุดของแต่ละ topic สำหรับ browser ที่เพิ่งต่อเข้ามา"""
        return dict(self.latest)

    def handle(self, topic, payload):
        if topic == HEARTRATE:
            bpm = payload.get("bpm")
            if not (is_number(bpm) and BPM_MIN <= bpm <= BPM_MAX):
                return Outcome(accepted=False)
            if self.state in PLAYING:
                self.numbers.bpm.append(bpm)
        elif topic == STATE:
            self._on_state(payload)
        elif topic == VISION and self.state == "RED_LIGHT":
            self.numbers.red_motion_max = _highest(self.numbers.red_motion_max, payload.get("motion_score"))
        elif topic == IMU and self.state == "RED_LIGHT":
            # ใช้เวลาตอนรับข้อความ: ค่าที่วัดช่วงผ่อนผันแต่มาถึงช้าอาจถูกนับรวม
            self._hold_red_sample(payload.get("acc_peak"))

        self.latest[topic] = payload
        if topic == RESULT:
            return Outcome(outgoing=self._summarise(payload))
        return Outcome()

    def _on_state(self, payload):
        round_id = payload.get("round_id")
        if round_id and round_id != self.round_id:
            self.round_id, self.state, self.numbers = round_id, None, RoundNumbers()
            for topic in (RESULT, SUMMARY, MEDIA):
                self.latest.pop(topic, None)
        state = payload.get("state")
        if self.state == "RED_LIGHT" and state != "RED_LIGHT":
            # ตัดรอยต่อเฉพาะตอนไป GREEN_LIGHT: ถ้าจบด้วย GAME_OVER ค่าท้ายสุดคือการขยับที่ทำให้แพ้
            before = self.clock() - self.margin if state == "GREEN_LIGHT" else None
            self._count_red_samples(arrived_before=before)
            self.numbers.red_acc_recent.clear()
        if state == "RED_LIGHT" and self.state != "RED_LIGHT":
            self.numbers.red_light_count += 1
        self.state = state

    def _hold_red_sample(self, value):
        now = self.clock()
        # ค่าที่เก่ากว่าช่วงรอยต่อแล้วไม่มีทางถูกตัด นับได้เลย
        self._count_red_samples(arrived_before=now - self.margin)
        self.numbers.red_acc_recent.append((now, value))

    def _count_red_samples(self, arrived_before=None):
        """นับค่าที่รอไว้ ถ้าระบุ arrived_before จะนับเฉพาะค่าที่มาถึงก่อนเวลานั้น"""
        waiting = []
        for arrived, value in self.numbers.red_acc_recent:
            if arrived_before is None or arrived <= arrived_before:
                self.numbers.red_acc_peak_max = _highest(self.numbers.red_acc_peak_max, value)
            else:
                waiting.append((arrived, value))
        self.numbers.red_acc_recent[:] = waiting

    def _summarise(self, result):
        round_id = result.get("round_id")
        # รอบที่ไม่เคยเห็น state (เช่น backend เพิ่งเปิดใหม่) ไม่มีข้อมูลพอจะสรุป
        if not round_id or round_id != self.round_id or round_id in self.summarised:
            return []
        self.summarised.add(round_id)
        self._count_red_samples()  # result อาจมาถึงก่อน state สุดท้าย
        numbers = self.numbers
        summary = {"round_id": round_id, "red_light_count": numbers.red_light_count}
        if numbers.bpm:
            summary["bpm_avg"] = round(sum(numbers.bpm) / len(numbers.bpm), 1)
            summary["bpm_max"] = max(numbers.bpm)
        if numbers.red_motion_max is not None:
            summary["red_motion_max"] = numbers.red_motion_max
        if numbers.red_acc_peak_max is not None:
            summary["red_acc_peak_max"] = numbers.red_acc_peak_max
        return [(SUMMARY, summary)]
