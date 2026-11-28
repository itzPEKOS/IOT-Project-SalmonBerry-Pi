"""รัน game engine: ต่อ broker บน Pi แล้วคุมเกมตาม engine/core.py

ในโฟลเดอร์ laptop:
    python -m engine                   อ่าน IP ของ broker จาก infra/.env (RLGL_MQTT_HOST)
    python -m engine --host 172.20.10.2
    python -m engine --skip-checks     เริ่มรอบได้แม้อุปกรณ์และ sensor ยังไม่ครบ (ไว้ทดสอบ)
    python -m engine --sensors camera,imu   บังคับ IMU ด้วย (ค่าเริ่มต้นบังคับแค่กล้อง ตัวอื่นใช้ไม่ได้ก็ยังเล่นต่อ)
    python -m engine --camera-only     ทดสอบกล้อง: ใช้กล้องตัดสินอย่างเดียว ไม่ต้องรอข้อมือ
    python -m engine --auto-lights     ไฟสลับเองตามเวลาสุ่ม (ไม่ต้องกดปุ่มเปลี่ยนไฟ)

คำสั่งในหน้าต่างนี้: Enter = ปุ่ม joystick (calibrate / เริ่มเล่น / ระหว่างเล่น: สลับไฟ), a = ยกเลิกรอบ, t = จำลองแตะเส้นชัย, s = ดูสถานะอุปกรณ์, q = ออก
"""
import argparse
import json
import os
import pathlib
import queue
import sys
import threading
import time

import paho.mqtt.client as mqtt

from engine.core import ACTIVE, BUTTON, CALIBRATION, CMD, RESULT, SENSORS, STATE, STATUS_PREFIX, SUBSCRIPTIONS, Config, Engine

DEVICE_ID = "game-01"
STATUS_TOPIC = STATUS_PREFIX + DEVICE_ID
TICK_S = 0.05
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / "infra" / ".env"


def load_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    values.update(os.environ)
    return values


def parse_args(env):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=env.get("RLGL_MQTT_HOST") or "127.0.0.1", help="IP ของ broker (Pi)")
    p.add_argument("--port", type=int, default=int(env.get("RLGL_MQTT_PORT") or 1883))
    p.add_argument("--calib", type=float, default=5.0, help="วินาทีที่ให้ยืนนิ่งตอน calibrate")
    p.add_argument("--green", type=float, nargs=2, default=(3.0, 6.0), metavar=("MIN", "MAX"))
    p.add_argument("--red", type=float, nargs=2, default=(3.0, 5.0), metavar=("MIN", "MAX"))
    p.add_argument("--transition", type=float, default=1.5, help="วินาทีที่กล้องหัน (ยังไม่จับ)")
    p.add_argument("--limit", type=float, default=30.0, help="เวลาเล่นต่อรอบ (วินาที)")
    p.add_argument("--auto-lights", action="store_true",
                   help="เกม 1: ไฟสลับเองตามเวลาสุ่ม (ค่าเริ่มต้น = คนคุมกดปุ่ม joystick / Enter เปลี่ยนไฟ)")
    p.add_argument("--camera-only", action="store_true",
                   help="ใช้กล้องอย่างเดียว: ตัดสินจากกล้อง, calibrate เช็กแค่กล้อง, ไม่ต้องรอข้อมือ online")
    p.add_argument("--intro", type=float, default=2.0, help="วินาทีของ animation ก่อนนับถอยหลัง")
    p.add_argument("--count", type=float, default=3.0, help="นับถอยหลังกี่วินาที")
    p.add_argument("--sensors", default=None,
                   help=f"sensor ที่ต้องใช้ได้ตอน calibrate คั่นด้วย , จาก {','.join(SENSORS)} (ใส่ none = ไม่เช็ก)")
    p.add_argument("--imu-acc", type=float, default=0.3, help="ไฟแดง: acc_peak (g) เกินนี้ = ขยับ")
    p.add_argument("--imu-gyro", type=float, default=50.0, help="ไฟแดง: gyro_rms (deg/s) เกินนี้ = ขยับ")
    p.add_argument("--skip-checks", action="store_true", help="ไม่ต้องรอ web/backend/Pi/ข้อมือ online")
    p.add_argument("--no-console", action="store_true", help="ไม่รับคำสั่งจากคีย์บอร์ด (เริ่มรอบด้วยปุ่มบนสถานีเท่านั้น)")
    args = p.parse_args()
    if args.sensors is None:
        args.sensors = "camera"  # ตัวอื่นเช็กและแสดงผล แต่ใช้ไม่ได้ก็ยังเล่นต่อ
    names = [] if args.sensors.strip().lower() == "none" else [n.strip().lower() for n in args.sensors.split(",") if n.strip()]
    unknown = [n for n in names if n not in SENSORS]
    if unknown:
        p.error(f"ไม่รู้จัก sensor: {', '.join(unknown)} (ใช้ได้: {', '.join(SENSORS)} หรือ none)")
    args.sensors = tuple(names)
    return args


def say(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


class Runner:
    def __init__(self, args, env):
        self.inbox = queue.Queue()
        self.seq = 0
        self.running = True
        self.last_state = None
        config = Config(calib_s=args.calib, green_s=tuple(args.green), red_s=tuple(args.red),
                        transition_s=args.transition, limit_s=args.limit, imu_acc_g=args.imu_acc,
                        imu_gyro_dps=args.imu_gyro, intro_s=args.intro, count_s=args.count,
                        sensors=() if args.skip_checks else args.sensors,
                        require=() if args.skip_checks else Config.require,
                        lights="auto" if args.auto_lights else "button",
                        judge=("camera",) if args.camera_only else Config.judge)
        self.engine = Engine(config, publish=self.publish)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{DEVICE_ID}-{os.getpid()}")
        if env.get("MQTT_USER"):
            self.client.username_pw_set(env["MQTT_USER"], env.get("MQTT_PASSWORD"))
        self.client.will_set(STATUS_TOPIC, self._wrap({"status": "offline"}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 5)
        self.announced = False
        self.host, self.port = args.host, args.port

    # ---------- MQTT (ทำงานบน thread ของ paho: ส่งต่อเข้า inbox อย่างเดียว) ----------

    def _wrap(self, body):
        self.seq += 1
        return json.dumps({"device_id": DEVICE_ID, "seq": self.seq, "ts": int(time.time() * 1000), **body},
                          ensure_ascii=False)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            say(f"ต่อ broker ไม่ได้: {reason_code}")
            return
        client.subscribe([(topic, 1) for topic in SUBSCRIPTIONS])
        client.publish(STATUS_TOPIC, self._wrap({"status": "online"}), qos=1, retain=True)
        self.inbox.put(("connected", None, None))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.inbox.put(("disconnected", None, None))

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(payload, dict) and payload.get("device_id") != DEVICE_ID:
            self.inbox.put(("message", message.topic, payload))

    def publish(self, topic, body, qos=0, retain=False):
        self.client.publish(topic, self._wrap(body), qos=qos, retain=retain)
        if topic == STATE and body["state"] != self.last_state:
            self.last_state = body["state"]
            extra = f" ({body['reason']})" if body.get("reason") else ""
            say(f"รอบ {body['round_id']}: {body['state']}{extra}")
        elif topic == CALIBRATION and body.get("done"):
            checks = "  ".join(f"{name}={body[f'check_{name}']}" for name in SENSORS)
            if body.get("ok"):
                say(f"calibrate ผ่าน: {checks}  → กด Enter อีกครั้งเพื่อเริ่มเล่น")
            else:
                say(f"calibrate ไม่ผ่าน (ใช้ไม่ได้: {body['failed']}): {checks}  → แก้แล้วกด Enter เพื่อ calibrate ใหม่")
        elif topic == RESULT:
            details = ", ".join(f"{k}={body[k]}" for k in ("duration_s", "caught_part", "caught_score_ratio",
                                                          "caught_red_s", "hits", "dodges", "bpm_rest") if k in body)
            say(f"ผล: {body['result']} เพราะ {body['reason']}  {details}")

    # ---------- คำสั่งจากคีย์บอร์ด ----------

    def console(self):
        commands = {"": "start", "g": "green", "r": "red", "a": "abort", "t": "touch", "s": "status", "q": "quit"}
        while self.running:
            try:
                line = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if line in commands:
                self.inbox.put(("command", commands[line], None))
            else:
                print("Enter = ปุ่ม joystick (calibrate / เริ่มเล่น / สลับไฟ), r = ไฟแดง, g = ไฟเขียว, a = ยกเลิก, t = แตะเส้นชัย, s = สถานะ, q = ออก", flush=True)

    def _run_command(self, name):
        if name == "quit":
            self.running = False
        elif name == "status":
            devices = ", ".join(f"{d}={'online' if on else 'offline'}" for d, on in sorted(self.engine.devices.items()))
            missing = self.engine.missing()
            say(f"อุปกรณ์: {devices or '(ยังไม่ได้รับสถานะ)'}  โหมดถัดไป: {self.engine.mode}  state: {self.engine.state}")
            say(f"sensor ที่ต้องผ่านตอน calibrate: {', '.join(self.engine.c.sensors) or '(ไม่เช็ก)'}")
            say("พร้อมเริ่มรอบ" if not missing else "ยังขาด: " + ", ".join(missing))
        else:
            ok, detail = self.engine.command(name)
            if not ok:
                say(f"ทำไม่ได้: {detail}")

    # ---------- loop หลัก (thread เดียวที่แตะ engine) ----------

    def handle(self, kind, topic, payload):
        if kind == "message":
            # คำสั่งจากปุ่มบนสถานีหรือหน้าเว็บ: เรียกตรงเพื่อพิมพ์เหตุผลถ้าเริ่มไม่ได้
            if topic == BUTTON and payload.get("pressed"):
                self._run_command("start")
                return
            if topic == CMD and payload.get("cmd") in ("start", "abort", "touch", "green", "red"):
                self._run_command(payload["cmd"])
                return
            if topic.startswith(STATUS_PREFIX):
                device, online = topic[len(STATUS_PREFIX):], payload.get("status") == "online"
                if self.engine.devices.get(device) != online:
                    say(f"[{device}] {'online' if online else 'offline'}")
            self.engine.message(topic, payload)
        elif kind == "command":
            self._run_command(topic)
        elif kind == "connected":
            say(f"ต่อ broker {self.host}:{self.port} แล้ว")
            if not self.announced:  # ล้าง state เก่าครั้งเดียว ไม่ทำซ้ำตอนต่อใหม่กลางรอบ
                self.announced = True
                self.engine.announce()
        elif kind == "disconnected":
            say("หลุดจาก broker กำลังต่อใหม่…")
            if self.engine.state in ACTIVE:
                self.engine.abort("disconnect")

    def run(self, console=True):
        say(f"กำลังต่อ broker {self.host}:{self.port} …")
        self.client.connect_async(self.host, self.port, keepalive=10)
        self.client.loop_start()
        if console:
            threading.Thread(target=self.console, daemon=True).start()
            print("Enter = ปุ่ม joystick (calibrate / เริ่มเล่น / สลับไฟ), r = ไฟแดง, g = ไฟเขียว, a = ยกเลิก, t = แตะเส้นชัย, s = สถานะ, q = ออก (หรือ Ctrl+C)", flush=True)
            c = self.engine.c
            mode = []
            if c.lights == "button":
                mode.append(f"เปลี่ยนไฟด้วยปุ่ม: เขียว ─กด→ เตือน {c.transition_s:.1f} วิ → แดง ─กด→ เขียว")
            else:
                mode.append("ไฟสลับเองตามเวลา")
            if c.judge == ("camera",):
                mode.append("ตัดสินจากกล้องอย่างเดียว")
            mode.append(f"เช็ก sensor: {', '.join(c.sensors) or 'ไม่เช็ก'}")
            mode.append(f"เวลาเล่น {c.limit_s:.0f} วิ")
            say(" | ".join(mode))
        try:
            while self.running:
                deadline = time.monotonic() + TICK_S
                while True:
                    try:
                        self.handle(*self.inbox.get(timeout=max(0.0, deadline - time.monotonic())))
                    except queue.Empty:
                        break
                self.engine.tick()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if self.engine.state in ACTIVE:
            self.engine.abort("operator")
        self.engine.announce()  # ไม่ให้ state ของรอบที่ค้างอยู่ใน broker
        info = self.client.publish(STATUS_TOPIC, self._wrap({"status": "offline"}), qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            pass
        self.client.loop_stop()
        self.client.disconnect()
        say("ปิด game engine แล้ว")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    env = load_env()
    args = parse_args(env)
    Runner(args, env).run(console=not args.no_console)


if __name__ == "__main__":
    main()
