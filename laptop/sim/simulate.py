"""จำลอง ESP32 และ Pi: ส่งข้อมูลเกมเป็นรอบ ๆ เข้า MQTT ตาม docs/mqtt-contract.md

    python simulate.py --rounds 3                 # สุ่มผลแต่ละรอบ
    python simulate.py --rounds 1 --scenario win
    python simulate.py --backend http://127.0.0.1:8000   # ส่งคลิปทดสอบเข้า backend หลังจบแต่ละรอบ (ต้องมี ffmpeg)
    python simulate.py --fake-backend             # ส่ง summary แทน ใช้เฉพาะตอนไม่ได้เปิด backend

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
SCENARIOS = ["win", "camera", "imu", "timeout"]


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


def send_test_clip(base_url, round_id, token=None):
    """ทำคลิปทดสอบ 3 วินาที (H.264, yuv420p) แล้วส่งเข้า backend เหมือนที่ uploader บน Pi จะทำ"""
    import requests

    with tempfile.TemporaryDirectory() as folder:
        clip = pathlib.Path(folder) / f"{round_id}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=size=640x480:rate=15:duration=3", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)], check=True, timeout=60)
        with clip.open("rb") as handle:
            response = requests.post(f"{base_url.rstrip('/')}/clips", data={"round_id": round_id},
                                     files={"video": (clip.name, handle, "video/mp4")},
                                     headers={"X-Upload-Token": token} if token else {}, timeout=(5, 60))
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


class Round:
    def __init__(self, devices, scenario, speed, fake_backend):
        self.d, self.scenario, self.speed, self.fake_backend = devices, scenario, speed, fake_backend
        self.round_id = f"r{time.strftime('%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.limit, self.elapsed, self.distance = 60.0, 0.0, 280.0
        self.state, self.bpm = "IDLE", random.uniform(72, 84)
        self.bpm_seen, self.red_motion, self.red_acc, self.red_periods = [], [], [], 0

    def set_state(self, state):
        self.state = state
        self.red_periods += state == "RED_LIGHT"
        self.send_state()

    def send_state(self):
        self.d.send("gateway-01", "rlgl/game/state",
                    {"round_id": self.round_id, "state": self.state,
                     "remaining_s": max(0, round(self.limit - self.elapsed))}, qos=1, retain=True)

    def camera(self, facing, pan):
        self.d.send("station-01", "rlgl/station/camera", {"pan": pan, "tilt": 90, "facing": facing},
                    qos=1, retain=True)

    def run_for(self, seconds, moving_part=None):
        """เดินเวลาทีละ 50 ms: IMU 20 Hz, ระยะ 5 Hz, ชีพจรและ state 1 Hz"""
        for tick in range(int(seconds * 20)):
            walking = self.state == "GREEN_LIGHT"
            caught = moving_part and tick > seconds * 20 * 0.6
            acc = abs(random.gauss(0.35, 0.1)) if walking else abs(random.gauss(0.04, 0.015))
            gyro = abs(random.gauss(60, 20)) if walking else abs(random.gauss(3, 1.2))
            if caught and moving_part == "imu":
                acc, gyro = random.uniform(0.5, 0.9), random.uniform(50, 120)
            self.d.send("wrist-01", "rlgl/wrist/imu",
                        {"acc_rms": round(acc * 0.6, 3), "acc_peak": round(acc, 3), "gyro_rms": round(gyro, 1)})
            if self.state == "RED_LIGHT":
                self.red_acc.append(round(acc, 3))
            if walking and self.state != "IDLE":
                self.distance = max(45.0, self.distance - random.uniform(0.2, 0.9))
            if tick % 4 == 0:
                self.d.send("gateway-01", "rlgl/gateway/distance",
                            {"distance_cm": round(self.distance + random.gauss(0, 1.5), 1)})
            if tick % 2 == 0 and self.state == "RED_LIGHT":
                score = abs(random.gauss(0.012, 0.005))
                if caught and moving_part == "camera":
                    score = random.uniform(0.08, 0.2)
                self.red_motion.append(round(score, 4))
                self.d.send("gateway-01", "rlgl/gateway/vision",
                            {"motion_score": round(score, 4), "confidence": round(random.uniform(0.8, 0.98), 2),
                             "fps": round(random.gauss(18, 1.2), 1), "inference_ms": round(random.gauss(48, 5), 1)})
            if tick % 20 == 0:
                target = 125 if self.state == "RED_LIGHT" else 105 if walking else 80
                self.bpm += (target - self.bpm) * 0.2 + random.gauss(0, 2)
                bpm = int(min(180, max(45, self.bpm)))
                if self.state in ("GREEN_LIGHT", "TRANSITION", "RED_LIGHT"):
                    self.bpm_seen.append(bpm)
                self.d.send("wrist-01", "rlgl/wrist/heartrate", {"bpm": bpm})
                self.d.send("gateway-01", "rlgl/gateway/vision",
                            {"fps": round(random.gauss(18, 1.2), 1)})
                self.send_state()
            time.sleep(0.05 / self.speed)
            self.elapsed += 0.05

    def play(self):
        started = int(time.time() * 1000)
        self.camera("away", 20)
        self.set_state("CALIBRATE")
        self.run_for(5)
        self.elapsed = 0.0
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

        if self.scenario == "win":
            self.distance = 48.0
            self.d.send("gateway-01", "rlgl/gateway/finish", {"touched": 1, "round_id": self.round_id}, qos=1)
            outcome, reason, final = "WIN", "touch", "WIN"
        else:
            if self.scenario == "timeout":
                self.elapsed = self.limit
            outcome, reason, final = "GAME_OVER", self.scenario, "GAME_OVER"
        ended = int(time.time() * 1000)
        self.set_state(final)
        self.d.send("gateway-01", "rlgl/game/result",
                    {"round_id": self.round_id, "result": outcome, "reason": reason,
                     "started_at": started, "ended_at": ended,
                     "duration_s": round((ended - started) / 1000, 1)}, qos=1)
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


def main():
    env = load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--scenario", choices=SCENARIOS + ["random"], default="random")
    parser.add_argument("--speed", type=float, default=1.0, help="2 = ส่งเร็วขึ้นสองเท่า")
    parser.add_argument("--fake-backend", action="store_true")
    parser.add_argument("--backend", help="URL ของ backend เพื่อส่งคลิปทดสอบหลังจบรอบ")
    args = parser.parse_args()
    if args.backend and args.fake_backend:
        parser.error("ใช้ --backend หรือ --fake-backend อย่างใดอย่างหนึ่ง (ไม่งั้น summary จะซ้ำ)")
    if args.backend and not shutil.which("ffmpeg"):
        parser.error("--backend ต้องมี ffmpeg ใน PATH เพื่อสร้างคลิปทดสอบ")

    devices = Devices(args.host, args.port, env.get("MQTT_USER", ""), env.get("MQTT_PASSWORD", ""))
    try:
        for number in range(1, args.rounds + 1):
            scenario = random.choice(SCENARIOS) if args.scenario == "random" else args.scenario
            game = Round(devices, scenario, args.speed, args.fake_backend)
            print(f"รอบ {number}/{args.rounds} {game.round_id} ({scenario}) ...", flush=True)
            outcome, reason = game.play()
            print(f"  -> {outcome} ({reason})", flush=True)
            if args.backend:
                try:
                    token = os.environ.get("RLGL_UPLOAD_TOKEN")
                    print(f"  คลิป: {send_test_clip(args.backend, game.round_id, token)}", flush=True)
                except Exception as error:
                    print(f"  ส่งคลิปไม่สำเร็จ: {error}", flush=True)
            time.sleep(1 / args.speed)
    finally:
        devices.close()


if __name__ == "__main__":
    main()
