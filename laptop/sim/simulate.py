"""จำลอง ESP32 และ Pi: ส่งข้อมูลเกมเป็นรอบ ๆ เข้า MQTT ตาม docs/mqtt-contract.md

    python simulate.py --rounds 3                 # สุ่มผลแต่ละรอบ
    python simulate.py --rounds 1 --scenario win
    python simulate.py --mode sniper --scenario hits
    python simulate.py --backend http://127.0.0.1:8000   # ส่งคลิปทดสอบเข้า backend หลังจบแต่ละรอบ (ต้องมี ffmpeg)
    python simulate.py --fake-backend             # ส่ง summary แทน ใช้เฉพาะตอนไม่ได้เปิด backend
    python simulate.py --via-backend http://127.0.0.1:8000 --mode follow --rounds 5
        # ไม่ใช้ broker: ส่งเข้า backend ทาง /dev/publish (backend ต้องเปิด RLGL_DEV=1)
        # --mode follow = เล่นโหมดที่เลือกไว้บนหน้าเว็บ (ถามจาก /api/session ก่อนทุกรอบ)

อ่านบัญชี MQTT จาก ../infra/.env หรือจาก environment variable MQTT_USER, MQTT_PASSWORD
"""
import argparse
import json
import math
import os
import pathlib
import random
import shutil
import subprocess
import tempfile
import time
import uuid

import paho.mqtt.client as mqtt

DEVICES = ["wrist-01", "station-01", "gateway-01"]
SCENARIOS = {"rlgl": ["win", "camera", "imu", "timeout"], "sniper": ["win", "hits", "timeout"]}
BODY_PARTS = ["head", "left_arm", "right_arm", "torso", "left_leg", "right_leg"]
SNIPER_LIVES, LOCK_LIMIT_S = 3, 1.0


def load_env():
    path = pathlib.Path(__file__).resolve().parent.parent / "infra" / ".env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith("MQTT_")})
    return values


def send_test_clip(base_url, round_id, token=None, seconds=6, fps=15):
    """ทำ JPEG ต่อกัน (MJPEG) แล้วส่งเข้า backend เหมือนที่ Pi จะทำหลังจบรอบ (ต้องมี ffmpeg)"""
    import requests

    with tempfile.TemporaryDirectory() as folder:
        clip = pathlib.Path(folder) / f"{round_id}.mjpeg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc=size=640x480:rate={fps}:duration={seconds}", "-q:v", "5",
                        "-f", "mjpeg", str(clip)], check=True, timeout=60)
        with clip.open("rb") as handle:
            response = requests.post(f"{base_url.rstrip('/')}/clips", data={"round_id": round_id, "fps": str(fps)},
                                     files={"video": (clip.name, handle, "application/octet-stream")},
                                     headers={"X-Upload-Token": token} if token else {}, timeout=(5, 180))
        response.raise_for_status()
        return response.json()["url"]


class Devices:
    """หนึ่ง connection ต่อหนึ่งอุปกรณ์ เพื่อให้ LWT ของแต่ละตัวทำงานเหมือนจริง"""

    def __init__(self, host, port, user, password):
        self.clients, self.seq = {}, {}
        for device in DEVICES:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"sim-{device}-{uuid.uuid4().hex[:6]}")
            client.username_pw_set(user, password)
            topic = f"rlgl/status/{device}"
            client.will_set(topic, json.dumps({"device_id": device, "status": "offline"}), qos=1, retain=True)
            client.connect(host, port, keepalive=10)
            client.loop_start()
            deadline = time.time() + 10
            while not client.is_connected():
                if time.time() > deadline:
                    raise SystemExit(f"ต่อ MQTT ไม่ได้ ({host}:{port}) ตรวจว่า broker เปิดอยู่และรหัสถูกต้อง")
                time.sleep(0.05)
            self.clients[device], self.seq[device] = client, 0
            self.send(device, topic, {"status": "online"}, qos=1, retain=True)

    def send(self, device, topic, payload, qos=0, retain=False):
        self.seq[device] += 1
        message = {"device_id": device, "seq": self.seq[device], "ts": int(time.time() * 1000), **payload}
        return self.clients[device].publish(topic, json.dumps(message), qos=qos, retain=retain)

    def close(self):
        for device, client in self.clients.items():
            info = self.send(device, f"rlgl/status/{device}", {"status": "offline"}, qos=1, retain=True)
            info.wait_for_publish(timeout=5)
            client.disconnect()
            client.loop_stop()


class HttpDevices:
    """ใช้แทน Devices เมื่อไม่มี broker: ส่งข้อความรูปแบบเดียวกันเข้า backend ทาง /dev/publish"""

    def __init__(self, base_url):
        import requests

        self.base, self.http, self.seq = base_url.rstrip("/"), requests.Session(), {}
        for device in DEVICES:
            self.send(device, f"rlgl/status/{device}", {"status": "online"}, qos=1, retain=True)

    def send(self, device, topic, payload, qos=0, retain=False):
        self.seq[device] = self.seq.get(device, 0) + 1
        message = {"device_id": device, "seq": self.seq[device], "ts": int(time.time() * 1000), **payload}
        response = self.http.post(f"{self.base}/dev/publish", json={"topic": topic, "payload": message}, timeout=5)
        if response.status_code == 404:
            raise SystemExit("backend ไม่เปิด /dev/publish: ต้องรัน backend ด้วย RLGL_DEV=1")
        response.raise_for_status()

    def close(self):
        for device in DEVICES:
            self.send(device, f"rlgl/status/{device}", {"status": "offline"}, qos=1, retain=True)


class Round:
    def __init__(self, devices, scenario, speed, fake_backend, mode="rlgl"):
        self.d, self.scenario, self.speed, self.fake_backend = devices, scenario, speed, fake_backend
        self.mode = mode
        self.round_id = f"r{time.strftime('%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.limit, self.elapsed, self.distance = 60.0, 0.0, 280.0
        self.state, self.bpm = "IDLE", random.uniform(72, 84)
        self.bpm_seen, self.red_motion, self.red_acc, self.red_periods = [], [], [], 0
        self.bpm_rest = None
        self.red_started = 0.0  # เวลา (elapsed) ที่ไฟแดงล่าสุดเริ่ม
        self.caught = None      # (part, seconds into red, score ratio) ตอนโดนจับด้วยกล้อง
        self.lives, self.hits, self.dodges, self.lock_s = SNIPER_LIVES, 0, 0, 0.0

    def set_state(self, state):
        self.state = state
        self.red_periods += state == "RED_LIGHT"
        if state == "RED_LIGHT":
            self.red_started = self.elapsed
        self.send_state()

    def send_state(self):
        body = {"round_id": self.round_id, "mode": self.mode, "state": self.state,
                "remaining_s": max(0, round(self.limit - self.elapsed))}
        if self.mode == "sniper":
            body.update(lives=self.lives, hits=self.hits, dodges=self.dodges)
        self.d.send("gateway-01", "rlgl/game/state", body, qos=1, retain=True)

    def camera(self, facing, pan):
        self.d.send("station-01", "rlgl/station/camera", {"pan": pan, "tilt": 90, "facing": facing},
                    qos=1, retain=True)

    def run_for(self, seconds, moving_part=None, sniper=None):
        """เดินเวลาทีละ 50 ms: IMU 20 Hz, ระยะ 5 Hz, ภาพ 10 Hz, ชีพจรและ state 1 Hz
        sniper = แผนของรอบ sniper: "dodge" (หลบได้) หรือ "stay" (ยืนในเป้าจนโดนยิง)"""
        ticks = int(seconds * 20)
        for tick in range(ticks):
            walking = self.state in ("GREEN_LIGHT", "SNIPER")
            caught = moving_part and tick > ticks * 0.6
            acc = abs(random.gauss(0.35, 0.1)) if walking else abs(random.gauss(0.04, 0.015))
            gyro = abs(random.gauss(60, 20)) if walking else abs(random.gauss(3, 1.2))
            if caught and moving_part == "imu":
                acc, gyro = random.uniform(0.5, 0.9), random.uniform(50, 120)
            self.d.send("wrist-01", "rlgl/wrist/imu",
                        {"acc_rms": round(acc * 0.6, 3), "acc_peak": round(acc, 3), "gyro_rms": round(gyro, 1)})
            if self.state == "RED_LIGHT":
                self.red_acc.append(round(acc, 3))
            if walking:
                self.distance = max(45.0, self.distance - random.uniform(0.2, 0.9))
            if tick % 4 == 0:
                self.d.send("gateway-01", "rlgl/gateway/distance",
                            {"distance_cm": round(self.distance + random.gauss(0, 1.5), 1)})
            if self.state == "CALIBRATE" and tick % 10 == 0:
                self.send_calibration(min(1.0, (tick + 10) / ticks), seconds)
            if tick % 2 == 0 and self.state == "RED_LIGHT":
                score = abs(random.gauss(0.012, 0.005))
                if caught and moving_part == "camera":
                    score = random.uniform(0.08, 0.2)
                    if self.caught is None:
                        self.caught = (random.choice(BODY_PARTS), round(self.elapsed - self.red_started, 1),
                                       round(score / 0.05, 2))
                self.red_motion.append(round(score, 4))
                self.d.send("gateway-01", "rlgl/gateway/vision",
                            {"motion_score": round(score, 4), "score_ratio": round(score / 0.05, 3),
                             "confidence": round(random.uniform(0.8, 0.98), 2),
                             "fps": round(random.gauss(18, 1.2), 1), "inference_ms": round(random.gauss(48, 5), 1)})
            if sniper and tick % 2 == 0 and self.aim(sniper, 0.1):
                return True  # โดนยิง: ให้ play() ตัดสินต่อ
            if tick % 20 == 0:
                target = 125 if self.state in ("RED_LIGHT", "SNIPER") else 105 if walking else 80
                self.bpm += (target - self.bpm) * 0.2 + random.gauss(0, 2)
                bpm = int(min(180, max(45, self.bpm)))
                if self.state in ("GREEN_LIGHT", "TRANSITION", "RED_LIGHT", "SNIPER"):
                    self.bpm_seen.append(bpm)
                if self.state == "CALIBRATE":
                    self.bpm_rest = bpm
                if sniper and random.random() < 0.35:
                    self.dodges += 1
                self.d.send("wrist-01", "rlgl/wrist/heartrate", {"bpm": bpm})
                self.d.send("gateway-01", "rlgl/gateway/vision",
                            {"fps": round(random.gauss(18, 1.2), 1)})
                self.send_state()
            time.sleep(0.05 / self.speed)
            self.elapsed += 0.05
        return False

    def send_calibration(self, progress, seconds):
        """ข้อเสนอ topic rlgl/game/calibration: ความคืบหน้าของช่วง calibrate"""
        self.d.send("gateway-01", "rlgl/game/calibration",
                    {"round_id": self.round_id, "progress": round(progress, 2), "player_detected": 1,
                     "keypoints": 17, "camera_baseline_s": round(progress * seconds, 1),
                     "imu_baseline_s": round(progress * seconds, 1), "baseline_s": seconds})

    def aim(self, plan, step_s):
        """คนคุม joystick เล็งกล้อง: ผู้เล่นอยู่ในกรอบครบ LOCK_LIMIT_S = โดนยิง 1 ครั้ง"""
        if plan == "stay":
            in_box = random.random() < 0.85
        else:
            in_box = random.random() < 0.45
        self.lock_s = round(self.lock_s + step_s, 2) if in_box else max(0.0, round(self.lock_s - step_s * 2, 2))
        hit = self.lock_s >= LOCK_LIMIT_S
        self.d.send("gateway-01", "rlgl/sniper/aim",
                    {"round_id": self.round_id, "in_box": int(in_box), "lock_s": min(self.lock_s, LOCK_LIMIT_S),
                     "lock_limit_s": LOCK_LIMIT_S, "part": "torso",
                     "offset_px": round(random.uniform(5, 40) if in_box else random.uniform(90, 220))})
        pan = int(90 + random.gauss(0, 12))
        self.d.send("station-01", "rlgl/station/camera",
                    {"pan": pan, "tilt": int(90 + random.gauss(0, 4)), "facing": "player"}, qos=1, retain=True)
        if hit:
            self.hits, self.lives, self.lock_s = self.hits + 1, self.lives - 1, 0.0
            self.send_state()
        return hit

    def play(self):
        return self.play_sniper() if self.mode == "sniper" else self.play_rlgl()

    def calibrate(self):
        started = int(time.time() * 1000)
        self.set_state("CALIBRATE")
        self.run_for(5)
        self.elapsed = 0.0
        return started

    def play_rlgl(self):
        self.camera("away", 20)
        started = self.calibrate()
        cycles = {"win": 3, "camera": 2, "imu": 2, "timeout": 3}[self.scenario]
        for cycle in range(cycles):
            self.camera("away", 20)
            self.set_state("GREEN_LIGHT")
            self.run_for(random.uniform(3, 5))
            if cycle == cycles - 1 and self.scenario == "win":
                break
            self.camera("turning", 90)
            self.set_state("TRANSITION")
            self.run_for(1.0)
            self.camera("player", 160)
            self.set_state("RED_LIGHT")
            last = cycle == cycles - 1 and self.scenario in ("camera", "imu")
            self.run_for(random.uniform(3, 4), moving_part=self.scenario if last else None)

        extra = {}
        if self.scenario == "win":
            self.distance = 48.0
            self.d.send("gateway-01", "rlgl/gateway/finish", {"touched": 1, "round_id": self.round_id}, qos=1)
            outcome, reason, final = "WIN", "touch", "WIN"
        else:
            if self.scenario == "timeout":
                self.elapsed = self.limit
            outcome, reason, final = "GAME_OVER", self.scenario, "GAME_OVER"
            if self.scenario == "camera" and self.caught:
                part, into_red, ratio = self.caught
                extra = {"caught_part": part, "caught_red_s": into_red, "caught_score_ratio": ratio}
            elif self.scenario == "imu":
                extra = {"caught_red_s": round(self.elapsed - self.red_started, 1)}
        return self.finish(started, outcome, reason, final, extra)

    def play_sniper(self):
        self.camera("player", 90)
        started = self.calibrate()
        self.set_state("SNIPER")
        plan = {"win": ["dodge", "stay", "dodge"], "hits": ["stay"] * SNIPER_LIVES, "timeout": ["dodge"]}
        outcome = None
        for leg in plan[self.scenario]:
            seconds = 60.0 if self.scenario == "timeout" else random.uniform(4, 7)
            seconds = min(seconds, self.limit - self.elapsed)
            if seconds <= 0:
                break
            hit = self.run_for(seconds, sniper=leg)
            if hit and self.lives <= 0:
                outcome = ("GAME_OVER", "hits")
                break
            if leg == "stay" and not hit:  # ยังไม่โดน ยืนต่อจนโดน
                self.run_for(3, sniper="stay")
        if outcome is None:
            if self.scenario == "win":
                self.run_for(2, sniper="dodge")
                self.distance = 48.0
                self.d.send("gateway-01", "rlgl/gateway/finish", {"touched": 1, "round_id": self.round_id}, qos=1)
                outcome = ("WIN", "touch")
            else:
                self.elapsed = self.limit
                outcome = ("GAME_OVER", "timeout")
        result, reason = outcome
        return self.finish(started, result, reason, result, {"hits": self.hits, "dodges": self.dodges})

    def finish(self, started, outcome, reason, final, extra):
        ended = int(time.time() * 1000)
        self.set_state(final)
        if self.bpm_rest is not None:
            extra["bpm_rest"] = self.bpm_rest
        self.d.send("gateway-01", "rlgl/game/result",
                    {"round_id": self.round_id, "mode": self.mode, "result": outcome, "reason": reason,
                     "started_at": started, "ended_at": ended,
                     "duration_s": round(min(self.elapsed, self.limit), 1), **extra}, qos=1)
        if self.fake_backend:
            summary = {"round_id": self.round_id, "red_light_count": self.red_periods}
            if self.bpm_seen:
                summary["bpm_avg"] = round(sum(self.bpm_seen) / len(self.bpm_seen), 1)
                summary["bpm_max"] = max(self.bpm_seen)
            if self.red_motion:
                summary["red_motion_max"] = max(self.red_motion)
            if self.red_acc:
                summary["red_acc_peak_max"] = max(self.red_acc)
            self.d.send("gateway-01", "rlgl/game/summary", summary, qos=1)
        return outcome, reason


def current_web_mode(base_url):
    """โหมดที่เลือกไว้บนหน้าเว็บ (เหมือน game engine ที่อ่าน rlgl/game/mode)"""
    import requests

    return requests.get(f"{base_url.rstrip('/')}/api/session", timeout=5).json().get("mode", "rlgl")


def main():
    env = load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--mode", choices=["rlgl", "sniper", "follow"], default="rlgl")
    parser.add_argument("--scenario", default="random",
                        help="rlgl: win camera imu timeout / sniper: win hits timeout / random")
    parser.add_argument("--speed", type=float, default=1.0, help="2 = ส่งเร็วขึ้นสองเท่า")
    parser.add_argument("--fake-backend", action="store_true")
    parser.add_argument("--backend", help="URL ของ backend เพื่อส่งคลิปทดสอบหลังจบรอบ")
    parser.add_argument("--via-backend", help="ไม่ใช้ broker: ส่งข้อความเข้า backend นี้ทาง /dev/publish")
    parser.add_argument("--gap", type=float, default=1.0, help="พักระหว่างรอบ (วินาที)")
    args = parser.parse_args()
    backend = args.backend or args.via_backend
    if args.backend and args.fake_backend:
        parser.error("ใช้ --backend หรือ --fake-backend อย่างใดอย่างหนึ่ง (ไม่งั้น summary จะซ้ำ)")
    if args.mode == "follow" and not backend:
        parser.error("--mode follow ต้องใช้คู่กับ --backend หรือ --via-backend")
    known = set(SCENARIOS["rlgl"]) | set(SCENARIOS["sniper"]) | {"random"}
    if args.scenario not in known:
        parser.error(f"--scenario ต้องเป็นหนึ่งใน {sorted(known)}")
    clips = bool(args.backend) or bool(args.via_backend and shutil.which("ffmpeg"))
    if args.backend and not shutil.which("ffmpeg"):
        parser.error("--backend ต้องมี ffmpeg ใน PATH เพื่อสร้างคลิปทดสอบ")

    if args.via_backend:
        devices = HttpDevices(args.via_backend)
    else:
        devices = Devices(args.host, args.port, env.get("MQTT_USER", ""), env.get("MQTT_PASSWORD", ""))
    try:
        for number in range(1, args.rounds + 1):
            mode = current_web_mode(backend) if args.mode == "follow" else args.mode
            choices = SCENARIOS[mode]
            scenario = random.choice(choices) if args.scenario == "random" else args.scenario
            if scenario not in choices:
                scenario = random.choice(choices)
            game = Round(devices, scenario, args.speed, args.fake_backend, mode)
            print(f"รอบ {number}/{args.rounds} {game.round_id} ({mode} {scenario}) ...", flush=True)
            outcome, reason = game.play()
            print(f"  -> {outcome} ({reason})", flush=True)
            if clips:
                try:
                    token = os.environ.get("RLGL_UPLOAD_TOKEN")
                    print(f"  คลิป: {send_test_clip(backend, game.round_id, token)}", flush=True)
                except Exception as error:
                    print(f"  ส่งคลิปไม่สำเร็จ: {error}", flush=True)
            time.sleep(args.gap / args.speed)
    finally:
        devices.close()


if __name__ == "__main__":
    main()
