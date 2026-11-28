"""กติกาของเกม (ไม่ผูกกับ MQTT): รับข้อความจากอุปกรณ์ ตัดสินแพ้ชนะ แล้วส่ง state และผลออกทาง publish

กดปุ่มครั้งแรก:  IDLE → CALIBRATE (ยืนนิ่ง + เช็ก sensor)
                  ├─ ผ่าน → READY
                  └─ ไม่ผ่าน → CALIBRATE_FAILED (กดอีกครั้ง = calibrate ใหม่ รอบเดิม)
กดปุ่มครั้งที่สอง: READY → COUNTDOWN (animation + นับ 3-2-1) → เล่น
โหมด rlgl:   GREEN_LIGHT → TRANSITION → RED_LIGHT → GREEN_LIGHT … → WIN / GAME_OVER
             ค่าเริ่มต้น: คนคุมกดปุ่ม joystick เปลี่ยนไฟ (เขียว ─กด→ เตือน 1.5 วิ → แดง ─กด→ เขียว)
             lights="auto": สลับเองตามเวลาสุ่ม
โหมด sniper: SNIPER → WIN / GAME_OVER
WIN / GAME_OVER ค้างจนรอบถัดไปเริ่ม ยกเลิกรอบ (ABORTED) แล้วกลับ IDLE ทันที

เวลาทั้งหมดเป็นวินาทีจาก clock() (ค่าเริ่มต้น time.time) และรอบเดินด้วยการเรียก tick() ถี่ๆ (เช่น 20 ครั้ง/วินาที)
"""
import random
import time
from dataclasses import dataclass

STATE = "rlgl/game/state"
RESULT = "rlgl/game/result"
CALIBRATION = "rlgl/game/calibration"
MODE = "rlgl/game/mode"
VISION = "rlgl/gateway/vision"
FINISH = "rlgl/gateway/finish"
DISTANCE = "rlgl/gateway/distance"
IMU = "rlgl/wrist/imu"
HEARTRATE = "rlgl/wrist/heartrate"
BUTTON = "rlgl/station/button"
AIM = "rlgl/sniper/aim"
CMD = "rlgl/cmd/game"
STATUS_PREFIX = "rlgl/status/"
SUBSCRIPTIONS = [MODE, VISION, FINISH, DISTANCE, IMU, HEARTRATE, BUTTON, AIM, CMD, STATUS_PREFIX + "+"]

MODES = ("rlgl", "sniper")
# สคริปต์บน Pi รายงานสถานะเป็น rlgl/status/pi (pi_client.py) ตาม contract คือ gateway-01 รับทั้งสองชื่อ
CAMERA_IDS = ("pi", "gateway-01")
STATION_ID = "station-01"
FINISH_ID = "finish-01"  # ESP ที่เส้นชัย: touch + ultrasonic
DEFAULT_REQUIRE = ("backend-01", "web-01", "pi", "wrist-01")
# sensor ที่เช็กตอน calibrate: imu, heart = ESP ข้อมือ / camera = Pi / distance, touch = ESP เส้นชัย (finish-01)
# station = ESP สถานี (joystick, servo)
SENSORS = ("imu", "heart", "camera", "distance", "touch", "station")
PLAYING = ("GREEN_LIGHT", "TRANSITION", "RED_LIGHT", "SNIPER")
ACTIVE = ("CALIBRATE", "CALIBRATE_FAILED", "READY", "COUNTDOWN", *PLAYING)
BPM_MIN, BPM_MAX = 45, 180
EPS = 1e-3  # กันเวลาทศนิยมคลาดนิดเดียว (epoch วินาทีมีเลขหลักใหญ่) แล้วเปลี่ยนไฟช้าไปหนึ่ง tick


@dataclass
class Config:
    calib_s: float = 5.0
    intro_s: float = 2.0             # animation ก่อนนับถอยหลัง
    count_s: float = 3.0             # นับ 3-2-1
    green_s: tuple = (3.0, 6.0)      # สุ่มความยาวไฟเขียวในช่วงนี้ (ผู้เล่นเดาจังหวะไม่ได้)
    transition_s: float = 1.5        # กล้องหัน = ช่วงผ่อนผัน ยังไม่จับ
    red_s: tuple = (3.0, 5.0)
    limit_s: float = 30.0            # เวลาเล่นทั้งรอบ นับจากไฟเขียวแรก (sniper: จากเริ่ม SNIPER)
    imu_acc_g: float = 0.3           # ไฟแดง: acc_peak เกินนี้ = ขยับ
    imu_gyro_dps: float = 50.0       # ไฟแดง: gyro_rms เกินนี้ = ขยับ
    imu_samples: int = 2             # ต้องเกินติดกันกี่ค่า (ESP ส่ง 20 ค่า/วินาที)
    imu_grace_s: float = 0.3         # ไม่นับค่าข้อมือช่วงแรกของไฟแดง (ค่าที่วัดตอนหันกล้องอาจมาถึงช้า)
    lives: int = 3
    lock_limit_s: float = 1.0        # sniper: อยู่ในกรอบครบเท่านี้ = โดนยิง (ถ้า Pi ไม่ส่ง lock_limit_s มา)
    dodge_acc_g: float = 0.6         # sniper: acc_peak เกินนี้ = หลบ 1 ครั้ง
    dodge_gap_s: float = 0.5         # sniper: หลบสองครั้งต้องห่างกันอย่างน้อยเท่านี้
    require: tuple = DEFAULT_REQUIRE  # อุปกรณ์ที่ต้อง online ก่อนเริ่ม และถ้าหลุดระหว่างเล่นจะยกเลิกรอบ
    sensors: tuple = ("imu", "heart", "camera")  # sensor ที่ต้องส่งข้อมูลจริงตอน calibrate ถึงจะเล่นได้
    imu_min_samples: int = 20        # calibrate: ต้องได้ IMU อย่างน้อยกี่ค่า
    camera_fresh_s: float = 2.0      # calibrate: กล้องต้องเห็นผู้เล่นภายในกี่วินาทีล่าสุด
    # เกม 1: "button" = คนคุมกดปุ่ม joystick เปลี่ยนไฟ (เขียว → เตือน transition_s → แดง, แดง → เขียว)
    #         "auto" = สลับเองตามเวลาสุ่ม green_s / red_s
    lights: str = "button"
    judge: tuple = ("camera", "imu")  # ใช้อะไรตัดสินการขยับตอนไฟแดง ("camera",) = กล้องอย่างเดียว


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def motion_ratio(payload):
    """การขยับเทียบเกณฑ์ (1.0 = เกณฑ์) แต่ละรุ่นของสคริปต์ Pi ใช้ชื่อต่างกัน:
    contract = score_ratio, pi_client.py เดิม = score, pi_client.py 28 พ.ย. = motion_score"""
    for key in ("score_ratio", "score", "motion_score"):
        if _number(payload.get(key)):
            return payload[key]
    return None


class Engine:
    def __init__(self, config, publish, clock=time.time, rng=None):
        self.c = config
        self.publish = publish  # publish(topic, body, qos=0, retain=False)
        self.clock = clock
        self.rng = rng or random.Random()
        self.devices = {}
        self.mode = "rlgl"  # โหมดที่เลือกบนหน้าเว็บ (rlgl/game/mode) ใช้กับรอบถัดไป
        self.state = "IDLE"
        self.round_id = None
        self.last_round_id = 0
        self.player_seen_at = None  # เก็บข้ามรอบ: Pi ส่ง heartbeat ตลอด
        self._clear_round()

    def _clear_round(self):
        self.round_mode = self.mode
        self.started = self.play_t0 = self.phase_end = self.red_t0 = None
        self.ended_elapsed = None
        self.next_state_at = self.next_calibration_at = None
        self.calib_t0 = None
        self.failed = []
        self._clear_sensor_counts()
        self.imu_streak = 0
        self.caught = None      # ข้อความ moved จากกล้องในไฟแดงนี้
        self.imu_moved = False
        self.touched = False
        self.lives, self.hits, self.dodges = self.c.lives, 0, 0
        self.locked = False
        self.last_dodge = None
        self.dodge_armed = True

    def _clear_sensor_counts(self):
        self.bpm_rest = []
        self.imu_count = 0
        self.distance_count = 0

    # ---------- ข้อความเข้า ----------

    def message(self, topic, payload):
        now = self.clock()
        if topic.startswith(STATUS_PREFIX):
            self._on_status(topic[len(STATUS_PREFIX):], payload.get("status") == "online")
        elif topic == MODE:
            if payload.get("mode") in MODES:
                self.mode = payload["mode"]
        elif topic == BUTTON:
            if payload.get("pressed"):
                self.command("start")
        elif topic == CMD:
            if payload.get("cmd") in ("start", "abort", "touch", "green", "red"):
                self.command(payload["cmd"])
        elif topic == HEARTRATE:
            bpm = payload.get("bpm")
            if self.state == "CALIBRATE" and _number(bpm) and BPM_MIN <= bpm <= BPM_MAX:
                self.bpm_rest.append(bpm)
        elif topic == DISTANCE:
            if self.state == "CALIBRATE" and _number(payload.get("distance_cm")):
                self.distance_count += 1
        elif topic == VISION:
            self._on_vision(payload, now)
        elif topic == IMU:
            self._on_imu(payload, now)
        elif topic == FINISH:
            if payload.get("touched") and self._same_round(payload) and self.state in PLAYING:
                self.touched = True
        elif topic == AIM:
            self._on_aim(payload)

    def _same_round(self, payload):
        round_id = payload.get("round_id")
        return round_id is None or str(round_id) == str(self.round_id)

    def _on_status(self, device, online):
        self.devices[device] = online
        if online or self.state not in ACTIVE:
            return
        if device in CAMERA_IDS and "pi" in self.c.require and not self._camera_online():
            self._end("ABORTED", "camera_lost")
        elif device in self.c.require and device != "pi":
            self._end("ABORTED", "disconnect")

    def _camera_online(self):
        return any(self.devices.get(device) for device in CAMERA_IDS)

    def _on_vision(self, payload, now):
        if payload.get("player_seen") or payload.get("valid"):
            self.player_seen_at = now
        if (payload.get("event") == "moved" and self.state == "RED_LIGHT" and self._same_round(payload)
                and self.caught is None and "camera" in self.c.judge):
            self.caught = payload

    def _on_imu(self, payload, now):
        if self.state == "CALIBRATE":
            self.imu_count += 1
            return
        acc, gyro = payload.get("acc_peak"), payload.get("gyro_rms")
        acc = acc if _number(acc) else 0.0
        gyro = gyro if _number(gyro) else 0.0
        if self.state == "RED_LIGHT":
            if "imu" not in self.c.judge or now - self.red_t0 < self.c.imu_grace_s:
                return
            over = acc > self.c.imu_acc_g or gyro > self.c.imu_gyro_dps
            self.imu_streak = self.imu_streak + 1 if over else 0
            if self.imu_streak >= self.c.imu_samples:
                self.imu_moved = True
        elif self.state == "SNIPER":
            fast = acc >= self.c.dodge_acc_g
            if fast and self.dodge_armed and (self.last_dodge is None or now - self.last_dodge >= self.c.dodge_gap_s):
                self.dodges += 1
                self.last_dodge = now
                self.dodge_armed = False
            elif not fast:
                self.dodge_armed = True

    def _on_aim(self, payload):
        if self.state != "SNIPER" or not self._same_round(payload) or self.lives <= 0:
            return
        if payload.get("event") == "hit":
            self._hit()
            return
        lock_s = payload.get("lock_s")
        limit = payload.get("lock_limit_s")
        limit = limit if _number(limit) and limit > 0 else self.c.lock_limit_s
        if not _number(lock_s):
            return
        if lock_s >= limit - EPS:
            if not self.locked:
                self.locked = True
                self._hit()
        else:
            self.locked = False

    def _hit(self):
        self.hits += 1
        self.lives -= 1
        self._send_state()  # contract: ส่งทันทีเมื่อโดนยิง

    # ---------- เช็ก sensor ----------

    def _sensor_ok(self, name, now):
        if name == "imu":
            return self.imu_count >= self.c.imu_min_samples
        if name == "heart":
            return bool(self.bpm_rest)
        if name == "camera":
            return self.player_seen_at is not None and now - self.player_seen_at <= self.c.camera_fresh_s
        if name == "distance":
            return self.distance_count > 0
        if name == "touch":  # touch ส่งข้อมูลเฉพาะตอนแตะ จึงเช็กได้แค่ว่า ESP เส้นชัย online
            return bool(self.devices.get(FINISH_ID))
        if name == "station":
            return bool(self.devices.get(STATION_ID))
        return False

    def sensor_checks(self, done):
        """{ชื่อ sensor: ok / wait (ยังไม่ครบ) / fail (calibrate จบแล้วยังไม่ครบ) / off (ไม่บังคับและไม่มีข้อมูล)}"""
        now = self.clock()
        checks = {}
        for name in SENSORS:
            if self._sensor_ok(name, now):
                checks[name] = "ok"
            elif name in self.c.sensors:
                checks[name] = "fail" if done else "wait"
            else:
                checks[name] = "off"
        return checks

    # ---------- คำสั่ง ----------

    def missing(self):
        """อุปกรณ์ที่ต้องมีแต่ยังไม่ online"""
        names = []
        for device in self.c.require:
            ok = self._camera_online() if device == "pi" else self.devices.get(device)
            if not ok:
                names.append(device)
        return names

    def command(self, name):
        """คืน (สำเร็จไหม, round_id หรือเหตุผลที่ทำไม่ได้)"""
        if name == "start":
            return self._press()
        if name == "abort":
            return self.abort("operator")
        if name == "touch":
            self.message(FINISH, {"touched": 1})
            return True, self.round_id
        if name in ("green", "red"):
            return self._light_key(name)
        return False, f"ไม่รู้จักคำสั่ง {name}"

    def _press(self):
        """ปุ่มเดียว: เริ่ม calibrate / calibrate ใหม่ / เริ่มนับถอยหลัง ตาม state ตอนนี้"""
        if self.state == "READY":
            self._set("COUNTDOWN", self.c.intro_s + self.c.count_s)
            return True, self.round_id
        if self.state in ("GREEN_LIGHT", "TRANSITION", "RED_LIGHT") and self._button_lights():
            return self._toggle_light()
        if self.state not in ("IDLE", "WIN", "GAME_OVER", "CALIBRATE_FAILED"):
            return False, f"กำลัง {self.state} รอบ {self.round_id} อยู่"
        missing = self.missing()
        if missing:
            return False, "ยังไม่ online: " + ", ".join(missing)
        if self.state == "CALIBRATE_FAILED":
            self._calibrate()  # รอบเดิม: ยังไม่ได้เริ่มเล่น
        else:
            self._begin()
        return True, self.round_id

    def _button_lights(self):
        return self.c.lights == "button" and self.round_mode == "rlgl"

    def _toggle_light(self):
        """ปุ่ม joystick ระหว่างเล่นเกม 1: เขียว → เตือน (เหลือง) → แดง, แดง → เขียว"""
        if self.state == "GREEN_LIGHT":
            self._set("TRANSITION", self.c.transition_s)  # หมดเวลาแล้วเป็นแดงเอง (_next_phase)
            return True, self.round_id
        if self.state == "RED_LIGHT":
            self._set("GREEN_LIGHT")
            return True, self.round_id
        return False, "กำลังเตือนเปลี่ยนเป็นไฟแดง รอสักครู่"

    def _light_key(self, name):
        """ปุ่ม r / g บนคีย์บอร์ด (หรือ rlgl/cmd/game) ใช้แทนปุ่ม joystick แบบระบุสีได้"""
        if not self._button_lights():
            return False, "สลับไฟเองได้เฉพาะเกม 1 ที่ใช้ปุ่มเปลี่ยนไฟ (ไม่ได้เปิดด้วย --auto-lights)"
        if self.state not in ("GREEN_LIGHT", "TRANSITION", "RED_LIGHT"):
            return False, f"ยังไม่ได้เริ่มเล่น (state ตอนนี้ {self.state})"
        if name == "red":
            return self._toggle_light() if self.state == "GREEN_LIGHT" else (True, self.round_id)
        if self.state != "GREEN_LIGHT":
            self._set("GREEN_LIGHT")
        return True, self.round_id

    def abort(self, reason):
        """ยกเลิกรอบที่กำลังเล่น เช่น reason = operator หรือ disconnect (หลุดจาก broker)"""
        if self.state not in ACTIVE:
            return False, "ไม่มีรอบที่กำลังเล่น"
        self._end("ABORTED", reason)
        return True, self.round_id

    def announce(self):
        """ตอนเปิดโปรแกรม: ล้าง state เก่าที่ค้างใน broker (retained)"""
        self.state, self.round_id = "IDLE", None
        self._send_state()

    # ---------- เดินเวลา ----------

    def _begin(self):
        now = self.clock()
        self.round_id = max(int(now), self.last_round_id + 1)  # ตัวเลข ไม่ซ้ำแม้ปิดเปิดโปรแกรมใหม่
        self.last_round_id = self.round_id
        self._clear_round()
        self.started = now
        self._calibrate()

    def _calibrate(self):
        now = self.clock()
        self._clear_sensor_counts()
        self.failed = []
        self.calib_t0 = now
        self.next_calibration_at = now
        self._set("CALIBRATE", self.c.calib_s)

    def _set(self, state, duration=None):
        now = self.clock()
        self.state = state
        self.phase_end = None if duration is None else now + duration
        self.caught, self.imu_moved, self.imu_streak = None, False, 0
        if state == "RED_LIGHT":
            self.red_t0 = now
        self._send_state()

    def tick(self):
        now = self.clock()
        if self.state not in ACTIVE:
            return
        if self.state in PLAYING and self._judge(now):
            return
        if self.phase_end is not None and now >= self.phase_end - EPS:
            self._next_phase()
        elif self.state == "CALIBRATE" and now >= self.next_calibration_at - EPS:
            self._send_calibration(min(1.0, (now - self.calib_t0) / self.c.calib_s), done=False)
            self.next_calibration_at += 0.5
        if now >= self.next_state_at - EPS:
            self._send_state()

    def _judge(self, now):
        """ตัดสินแพ้ชนะ คืน True ถ้ารอบจบแล้ว"""
        if self.state == "RED_LIGHT" and (self.caught or self.imu_moved):
            reason = "both" if self.caught and self.imu_moved else "camera" if self.caught else "imu"
            self._end("GAME_OVER", reason)
        elif self.state == "SNIPER" and self.lives <= 0:
            self._end("GAME_OVER", "hits")
        elif self.touched:
            if self.state == "RED_LIGHT":
                self._end("GAME_OVER", "finish_during_red")
            else:
                self._end("WIN", "touch")
        elif self.play_t0 is not None and now - self.play_t0 >= self.c.limit_s - EPS:
            self._end("GAME_OVER", "timeout")
        else:
            return False
        return True

    def _next_phase(self):
        if self.state == "CALIBRATE":
            checks = self.sensor_checks(done=True)
            self.failed = [name for name in SENSORS if checks[name] == "fail"]
            self._send_calibration(1.0, done=True, checks=checks)
            self._set("CALIBRATE_FAILED" if self.failed else "READY")
        elif self.state == "COUNTDOWN":
            self.play_t0 = self.clock()
            if self.round_mode == "sniper":
                self._set("SNIPER")
            elif self._button_lights():
                self._set("GREEN_LIGHT")  # ค้างไว้จนคนคุมกดปุ่ม
            else:
                self._set("GREEN_LIGHT", self.rng.uniform(*self.c.green_s))
        elif self.state == "GREEN_LIGHT":
            self._set("TRANSITION", self.c.transition_s)
        elif self.state == "TRANSITION":
            self._set("RED_LIGHT", None if self._button_lights() else self.rng.uniform(*self.c.red_s))
        elif self.state == "RED_LIGHT":
            self._set("GREEN_LIGHT", self.rng.uniform(*self.c.green_s))

    def _elapsed(self):
        if self.ended_elapsed is not None:
            return self.ended_elapsed
        if self.play_t0 is None:
            return 0.0
        return min(self.clock() - self.play_t0, self.c.limit_s)

    # ---------- ข้อความออก ----------

    def _send_state(self, **extra):
        now = self.clock()
        body = {"round_id": self.round_id, "mode": self.round_mode if self.round_id else self.mode,
                "state": self.state, "remaining_s": max(0, round(self.c.limit_s - self._elapsed()))}
        if self.round_id and self.round_mode == "sniper":
            body.update(lives=self.lives, hits=self.hits, dodges=self.dodges)
        if self.state == "COUNTDOWN":
            # หน้าเว็บใช้ starts_in_s คำนวณเวลาไฟเขียว: intro_s แรกเป็น animation แล้วนับ count_s ถอยหลัง
            body.update(starts_in_s=round(max(0.0, self.phase_end - now), 1), intro_s=self.c.intro_s,
                        count_s=self.c.count_s)
        if self.state == "CALIBRATE_FAILED":
            body["failed"] = ",".join(self.failed)
        body.update(extra)
        self.publish(STATE, body, qos=1, retain=True)
        self.next_state_at = now + 1.0

    def _send_calibration(self, progress, done, checks=None):
        now = self.clock()
        checks = checks or self.sensor_checks(done=False)
        seen = self.player_seen_at is not None and now - self.player_seen_at <= self.c.camera_fresh_s
        elapsed = round(progress * self.c.calib_s, 1)
        body = {"round_id": self.round_id, "progress": round(progress, 2), "player_detected": int(seen),
                "camera_baseline_s": elapsed, "imu_baseline_s": elapsed, "baseline_s": self.c.calib_s,
                "done": int(done), "required": ",".join(self.c.sensors)}
        body.update({f"check_{name}": status for name, status in checks.items()})
        if done:
            failed = [name for name in SENSORS if checks[name] == "fail"]
            body.update(ok=int(not failed), failed=",".join(failed))
        self.publish(CALIBRATION, body)

    def _end(self, result, reason):
        now = self.clock()
        self.ended_elapsed = round(self._elapsed(), 1)
        body = {"round_id": self.round_id, "mode": self.round_mode, "result": result, "reason": reason,
                "started_at": int(self.started * 1000), "ended_at": int(now * 1000),
                "duration_s": self.ended_elapsed, "cause": reason}  # cause = ชื่อที่ pi_client.py อ่าน
        if self.bpm_rest:
            body["bpm_rest"] = round(sum(self.bpm_rest) / len(self.bpm_rest))
        if self.state == "RED_LIGHT" and reason in ("camera", "imu", "both"):
            body["caught_red_s"] = round(now - self.red_t0, 1)
        if self.caught and reason in ("camera", "both"):
            part, score = self.caught.get("part"), motion_ratio(self.caught)
            if part:
                body["caught_part"] = body["part"] = part
            if _number(score):
                body["caught_score_ratio"] = body["score"] = round(score, 2)
        if self.round_mode == "sniper":
            body.update(hits=self.hits, dodges=self.dodges)
        # ส่งผลก่อน state สุดท้าย: pi_client.py อ่านผลล่าสุดตอนได้ state GAME_OVER / WIN
        self.publish(RESULT, body, qos=1)
        self.phase_end = None
        # WIN / GAME_OVER ค้างไว้ (หน้าเว็บแสดงผลจนกว่าจะกด PLAY AGAIN หรือรอบใหม่เริ่ม) ยกเลิกรอบกลับ IDLE
        self.state = "IDLE" if result == "ABORTED" else result
        self._send_state(result=result, reason=reason)
