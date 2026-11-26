"""
pi_client.py: งานฝั่ง Pi (gateway) สำหรับโหมด RLGL
    กล้อง → MediaPipe Pose → motion_detector → MQTT  rlgl/gateway/vision
                                             → HTTP  ภาพสด MJPEG (มีโครงร่าง + จุดที่ขยับ)
    และฟัง rlgl/game/state จาก Game Engine เพื่อรู้ว่าเมื่อไหร่ calibrate / เริ่มจับไฟแดง

ทดสอบบน laptop (เว็บแคม):
    pip install paho-mqtt
    1) เปิด broker      : mosquitto -c mosquitto.conf -v
    2) เปิดเกมจำลอง     : python fake_game.py
    3) เปิดตัวนี้        : python pi_client.py --source 0
    4) เปิดเบราว์เซอร์   : http://localhost:8000
    ดูข้อความใน MQTT    : mosquitto_sub -h localhost -t "rlgl/#" -v

ไม่มี broker ก็ลองได้: python pi_client.py --source 0 --sim   (สลับไฟเองในเครื่อง ไม่ส่ง MQTT)
บน Pi จริง:          python pi_client.py --broker <IP laptop>          (กล้อง CSI เป็นค่าเริ่มต้น)

ข้อความที่ส่ง (ทุกข้อความมี device_id, seq, ts, round_id):
    rlgl/gateway/vision  QoS 0  ทุกเฟรมช่วง RED_LIGHT: moved, score, part, joint, joint_xy, valid, n_joints, confidence, fps, infer_ms
                         QoS 1  event="moved" ตอนจับได้ครั้งแรกของรอบ / event="calibrated" ตอน calibrate เสร็จ
                         QoS 0  นอกไฟแดงส่งสรุป fps ทุก 1 วินาที (event="heartbeat")
    rlgl/status/pi       QoS 1 retain  online / offline (offline ส่งโดย broker อัตโนมัติถ้าหลุด = LWT)
    rlgl/gateway/replay  QoS 1  คลิปรีเพลย์ของรอบอัปโหลดเสร็จแล้ว: url, page, moved_at, result, cause
รับ:
    rlgl/game/state      {"state": "CALIBRATE" | "GREEN_LIGHT" | "TRANSITION" | "RED_LIGHT" | "WIN" | "GAME_OVER" | "IDLE", "round_id": 7}
                         Game Engine ต้องส่ง RED_LIGHT หลังหมดช่วงผ่อนผันแล้ว (Pi เริ่มเก็บท่าตั้งต้นทันทีที่ได้รับ)
    rlgl/game/result     {"result": "GAME_OVER" | "WIN", "cause": "camera" | "wrist" | ..., "round_id": 7}

คลิปรีเพลย์ (HTTP):
    Pi เก็บภาพ (ที่วาดโครงร่างแล้ว) ย้อนหลัง ~12 วินาทีไว้ในหน่วยความจำตลอด
    พอจับได้ว่าขยับ หรือได้รับ GAME_OVER / WIN → เก็บต่ออีก --post วินาที แล้วตัดคลิป --pre วินาทีก่อน + --post วินาทีหลัง
    ได้ 2 ไฟล์ใน captures/replays/: วิดีโอ (H.264 .mp4 ถ้ามี ffmpeg, ไม่มีก็ .webm) + .json ข้อมูลรายเฟรม (moved_at = วินาทีที่ขยับในคลิป)
    แล้ว POST ไปที่ replay_server.py บน laptop (--replay-url) และแจ้ง rlgl/gateway/replay
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import cv2
import numpy as np

from motion_detector import JOINTS, DetectorConfig, MotionDetector
from test_motion import MediaPipePose, draw_mp_all, put

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

STATE_COLOR = {"GREEN_LIGHT": (0, 200, 0), "TRANSITION": (0, 200, 255), "RED_LIGHT": (0, 0, 255),
               "CALIBRATE": (255, 160, 0), "GAME_OVER": (0, 0, 255), "WIN": (0, 200, 0)}


# ---------------------------------------------------------------- camera
class Camera:
    """picam = กล้อง CSI บน Pi (picamera2), ตัวเลข = เว็บแคม/USB"""

    def __init__(self, source, w, h):
        self.picam = self.cap = None
        if source == "picam":
            from picamera2 import Picamera2
            self.picam = Picamera2()
            self.picam.configure(self.picam.create_video_configuration(main={"size": (w, h), "format": "RGB888"}))
            self.picam.start()
            time.sleep(1.0)
        else:
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(int(source), backend)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            if not self.cap.isOpened():
                raise RuntimeError(f"เปิดกล้อง {source} ไม่ได้ ลอง --source 1")

    def read(self):
        if self.picam is not None:
            return self.picam.capture_array()
        ok, f = self.cap.read()
        return f if ok else None

    def close(self):
        if self.picam is not None:
            self.picam.stop()
        if self.cap is not None:
            self.cap.release()


# ---------------------------------------------------------------- MQTT
class Link:
    """ห่อ paho-mqtt: ใส่ device_id/seq/ts ให้ทุกข้อความ, ตั้ง LWT, รับ game state"""

    def __init__(self, args):
        self.dev, self.seq, self.round_id = args.device_id, 0, None
        self.latest_state, self.latest_result, self.lock = None, None, threading.Lock()
        self.connected, self.cli = False, None
        if args.sim or mqtt is None:
            if mqtt is None and not args.sim:
                print("ไม่พบ paho-mqtt (pip install paho-mqtt) ทำงานแบบไม่ส่ง MQTT")
            return
        cid = f"{self.dev}-{os.getpid()}"
        try:
            self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:  # paho-mqtt 1.x
            self.cli = mqtt.Client(client_id=cid)
        self.status_topic = "rlgl/status/pi"
        self.cli.will_set(self.status_topic, self._wrap({"status": "offline"}), qos=1, retain=True)
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = lambda *a: setattr(self, "connected", False)
        self.cli.on_message = self._on_message
        self.cli.reconnect_delay_set(1, 5)
        self.cli.connect_async(args.broker, args.port, keepalive=10)
        self.cli.loop_start()
        print(f"MQTT → {args.broker}:{args.port} as {cid}")

    def _wrap(self, data):
        self.seq += 1
        return json.dumps({"device_id": self.dev, "seq": self.seq, "ts": int(time.time() * 1000),
                           "round_id": self.round_id, **data}, ensure_ascii=False)

    def _on_connect(self, c, *args):
        self.connected = True
        c.subscribe([("rlgl/game/state", 1), ("rlgl/game/result", 1)])
        c.publish(self.status_topic, self._wrap({"status": "online"}), qos=1, retain=True)
        print("MQTT connected")

    def _on_message(self, c, u, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        with self.lock:
            if msg.topic == "rlgl/game/result":
                self.latest_result = data
            else:
                self.latest_state = data

    def take_result(self):
        with self.lock:
            r, self.latest_result = self.latest_result, None
        return r

    def take_state(self):
        with self.lock:
            s, self.latest_state = self.latest_state, None
        return s

    def publish(self, topic, data, qos=0):
        if self.cli is not None:
            self.cli.publish(topic, self._wrap(data), qos=qos)

    def close(self):
        if self.cli is not None:
            self.cli.publish(self.status_topic, self._wrap({"status": "offline"}), qos=1, retain=True).wait_for_publish(2)
            self.cli.loop_stop()
            self.cli.disconnect()


class SimGame:
    """--sim: สลับไฟเองแทน Game Engine (ทดสอบภาพโดยไม่มี broker)"""
    CYCLE = [("GREEN_LIGHT", 4.0), ("TRANSITION", 1.5), ("RED_LIGHT", 4.0)]

    def __init__(self, calib):
        self.t0, self.calib, self.last, self.rid = time.perf_counter(), calib, None, 1

    def take_result(self):
        return None

    def take_state(self):
        el = time.perf_counter() - self.t0
        if el < self.calib:
            st = "CALIBRATE"
        else:
            x = (el - self.calib) % sum(d for _, d in self.CYCLE)
            self.rid = 1 + int((el - self.calib) // sum(d for _, d in self.CYCLE))
            for st, d in self.CYCLE:
                if x < d:
                    break
                x -= d
        if st == self.last:
            return None
        self.last = st
        return {"state": st, "round_id": self.rid}


# ---------------------------------------------------------------- replay
def write_video(jpgs, fps, base):
    """เขียนคลิปที่เบราว์เซอร์เล่นได้: ffmpeg → H.264 .mp4 (ดีที่สุด), ไม่มี ffmpeg → OpenCV VP8 .webm
    คืน (path, codec) หรือ (None, None)"""
    ff = shutil.which("ffmpeg")
    if ff:
        cmd = [ff, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", f"{fps:.2f}", "-c:v", "mjpeg", "-i", "-",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", base + ".mp4"]
        try:
            p = subprocess.run(cmd, input=b"".join(jpgs), capture_output=True, timeout=120)
            if p.returncode == 0 and os.path.getsize(base + ".mp4") > 1000:
                return base + ".mp4", "h264"
            print("ffmpeg error:", p.stderr.decode(errors="ignore")[-300:])
        except (OSError, subprocess.TimeoutExpired) as e:
            print("ffmpeg failed:", e)
    first = cv2.imdecode(np.frombuffer(jpgs[0], np.uint8), cv2.IMREAD_COLOR)
    h, w = first.shape[:2]
    for fourcc, ext in (("VP80", "webm"), ("avc1", "mp4"), ("mp4v", "mp4")):
        path = f"{base}.{ext}"
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
        if not vw.isOpened():
            continue
        for j in jpgs:
            vw.write(cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR))
        vw.release()
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            if fourcc == "mp4v":
                print("คำเตือน: ได้คลิป mp4v ซึ่งเบราว์เซอร์มักเล่นไม่ได้ ติดตั้ง ffmpeg เพื่อให้ได้ H.264")
            return path, fourcc.lower()
    return None, None


def upload(url, round_id, video_path, json_path):
    """POST ไฟล์คลิป + json ไปที่ replay_server.py คืน dict ที่ server ตอบ"""
    try:
        import requests
    except ImportError:
        raise RuntimeError("ไม่มี requests (pip install requests)")
    mime = "video/mp4" if video_path.endswith(".mp4") else "video/webm"
    with open(video_path, "rb") as v, open(json_path, "rb") as m:
        r = requests.post(url, data={"round_id": str(round_id)}, timeout=30,
                          files={"video": (os.path.basename(video_path), v, mime),
                                 "meta": (os.path.basename(json_path), m, "application/json")})
    r.raise_for_status()
    return r.json()


class ReplayRecorder:
    """เก็บเฟรมย้อนหลังในหน่วยความจำ แล้วตัดคลิปรอบละ 1 คลิปเมื่อมีเหตุการณ์ (ขยับ / GAME_OVER / WIN)"""

    def __init__(self, args, link):
        self.pre, self.post, self.url, self.link = args.pre, args.post, args.replay_url, link
        self.buf = deque()
        self.pending, self.done = None, set()
        self.last = None
        self.dir = os.path.join("captures", "replays")
        os.makedirs(self.dir, exist_ok=True)

    def add(self, t, jpg, meta):
        self.buf.append((t, jpg, meta))
        while self.buf and t - self.buf[0][0] > self.pre + self.post + 2:
            self.buf.popleft()

    def mark(self, t, round_id, info):
        """จดเวลาเหตุการณ์ (ครั้งแรกของรอบเท่านั้น)"""
        key = round_id if round_id is not None else "none"
        if self.pending is not None or key in self.done:
            return False
        self.pending = {"t": t, "round_id": round_id, **info}
        return True

    def poll(self, t):
        p = self.pending
        if p is None or t < p["t"] + self.post:
            return
        self.pending = None
        self.done.add(p["round_id"] if p["round_id"] is not None else "none")
        frames = [f for f in self.buf if p["t"] - self.pre <= f[0] <= p["t"] + self.post]
        if len(frames) >= 5:
            threading.Thread(target=self._finalize, args=(p, frames), daemon=True).start()

    def _finalize(self, p, frames):
        t0, t1 = frames[0][0], frames[-1][0]
        fps = (len(frames) - 1) / (t1 - t0) if t1 > t0 else 15.0
        rid = p["round_id"] if p["round_id"] is not None else 0
        base = os.path.join(self.dir, f"round_{rid:03d}_{time.strftime('%H%M%S')}")
        video, codec = write_video([f[1] for f in frames], fps, base)
        if not video:
            print("ทำคลิปไม่สำเร็จ")
            return
        meta = {"round_id": p["round_id"], "result": p.get("result"), "cause": p.get("cause"), "part": p.get("part"),
                "joint": p.get("joint"), "score": p.get("score"), "moved_at": round(p["t"] - t0, 3),
                "duration": round(t1 - t0, 3), "fps": round(fps, 2), "video": os.path.basename(video), "codec": codec,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "frames": [{"t": round(f[0] - t0, 3), **f[2]} for f in frames]}
        with open(base + ".json", "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False)
        print(f"replay saved: {video} ({len(frames)} frames, {fps:.1f} fps, event at {meta['moved_at']} s)")
        info = {"round_id": p["round_id"], "file": os.path.basename(video), "moved_at": meta["moved_at"],
                "result": meta["result"], "cause": meta["cause"], "uploaded": False}
        if self.url:
            try:
                res = upload(self.url, rid, video, base + ".json")
                info.update(uploaded=True, url=urljoin(self.url, res.get("url", "")), page=urljoin(self.url, res.get("page", "")))
                print("replay uploaded:", info["page"])
            except Exception as e:
                info["error"] = str(e)[:200]
                print("อัปโหลดคลิปไม่สำเร็จ (ไฟล์ยังอยู่ใน captures/replays):", e)
        if self.link is not None and info["uploaded"]:
            self.link.publish("rlgl/gateway/replay", info, qos=1)
        self.last = info


# ---------------------------------------------------------------- overlay
def draw_overlay(img, state, det, r, cur, caught, fps, infer_ms, connected):
    h, w = img.shape[:2]
    if det.mode == "judge" and det.ref is not None:
        ok = det.ref_ok
        for j in np.flatnonzero(ok):  # ท่าตั้งต้น (เส้นประสีฟ้า = จุดตอนเริ่มไฟแดง)
            x, y = det.ref[j]
            cv2.rectangle(img, (int(x) - 3, int(y) - 3), (int(x) + 3, int(y) + 3), (255, 200, 0), 1)
    if r is not None and r.valid and r.joint and cur is not None:
        j = JOINTS.index(r.joint)
        cx, cy = map(int, cur[0][j])
        color = (0, 0, 255) if r.score > 1 else (0, 200, 255) if r.score > 0.6 else (0, 200, 0)
        if det.ref is not None:
            rx, ry = map(int, det.ref[j])
            cv2.line(img, (rx, ry), (cx, cy), color, 2, cv2.LINE_AA)
        cv2.rectangle(img, (cx - 16, cy - 16), (cx + 16, cy + 16), color, 3)
        label = f"{r.part}/{r.joint} {r.score:.2f}"
        (tw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        lx = cx + 22 if cx + 22 + tw < w else cx - 22 - tw
        for col, th in (((0, 0, 0), 4), (color, 2)):
            cv2.putText(img, label, (lx, cy + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, th, cv2.LINE_AA)
    bar = STATE_COLOR.get(state, (120, 120, 120))
    cv2.rectangle(img, (0, 0), (w, 36), bar, -1)
    put(img, f"{state or 'WAITING FOR GAME STATE'}   FPS {fps:4.1f}   infer {infer_ms:4.0f} ms", 26, (255, 255, 255), 0.6)
    if det.mode == "ref":
        put(img, "building reference pose...", 62, (0, 200, 255))
    elif r is not None and r.mode == "judge":
        put(img, "no player" if not r.valid else f"score {r.score:.2f} / 1.00", 62,
            (0, 0, 255) if (r.score or 0) > 1 else (255, 255, 255))
    if caught:
        cv2.rectangle(img, (0, h - 44), (w, h), (0, 0, 255), -1)
        put(img, f"MOVED: {caught['part']} / {caught['joint']}  score {caught['score']:.2f}", h - 14, (255, 255, 255), 0.7)
    cv2.circle(img, (w - 16, 18), 7, (0, 220, 0) if connected else (0, 0, 255), -1)


# ---------------------------------------------------------------- app
class App:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.jpg, self.jseq = None, 0
        self.status = {"state": None}
        self.running = True

    def run(self):
        a = self.args
        link = SimGame(a.calib) if a.sim else Link(a)
        mq = None if a.sim else link
        det = MotionDetector(DetectorConfig(k_sigma=a.k))
        rec = None if a.no_replay else ReplayRecorder(a, mq)
        cam = Camera(a.source, a.width, a.height)
        pose = MediaPipePose(a.variant)
        state, rid, caught, r = None, None, None, None
        fps = infer_ms = 0.0
        t_prev = t_beat = time.perf_counter()
        os.makedirs("captures", exist_ok=True)
        try:
            while self.running:
                frame = cam.read()
                if frame is None:
                    time.sleep(0.05)
                    continue
                t0 = time.perf_counter()
                cur = pose(frame)
                dt = (time.perf_counter() - t0) * 1000
                infer_ms = 0.9 * infer_ms + 0.1 * dt if infer_ms else dt
                now = time.perf_counter()
                fps = 0.9 * fps + 0.1 / max(now - t_prev, 1e-6) if fps else 1 / max(now - t_prev, 1e-6)
                t_prev = now

                # ----- game state from engine (or --sim)
                msg = link.take_state()
                if msg and msg.get("state") and msg["state"] != state:
                    new = msg["state"]
                    rid = msg.get("round_id", rid)
                    if mq:
                        mq.round_id = rid
                    if state == "CALIBRATE":
                        info = det.finish_calibration()
                        print("calibrated:", {k: info.get(k) for k in ("frames", "noise_factor", "ok", "msg")})
                        if mq:
                            mq.publish("rlgl/gateway/vision", {"event": "calibrated", **{k: info.get(k) for k in ("frames", "noise_factor", "ok", "msg")}}, qos=1)
                    if state == "RED_LIGHT":
                        det.stop()
                    if new == "CALIBRATE":
                        det.start_calibration()
                    if new == "RED_LIGHT":
                        det.start_red()
                        caught = None
                    if new in ("GREEN_LIGHT", "CALIBRATE", "IDLE"):
                        caught = None
                    if new in ("GAME_OVER", "WIN") and rec is not None:
                        res = link.take_result() or msg
                        rec.mark(now, rid, {"result": new, "cause": res.get("cause") or ("finish" if new == "WIN" else None),
                                            "part": res.get("part"), "joint": res.get("joint"), "score": res.get("score")})
                    print(f"state: {state} → {new}  (round {rid})")
                    state = new

                r = det.update(now, cur)

                # ----- publish
                if state == "RED_LIGHT" and mq:
                    data = {"event": "frame", **r.to_dict(), "fps": round(fps, 1), "infer_ms": round(infer_ms, 1)}
                    if r.joint and cur is not None:
                        x, y = cur[0][JOINTS.index(r.joint)]
                        data["joint_xy"] = [round(float(x) / frame.shape[1], 3), round(float(y) / frame.shape[0], 3)]
                    mq.publish("rlgl/gateway/vision", data, qos=0)
                    if r.triggered:
                        mq.publish("rlgl/gateway/vision", {**data, "event": "moved"}, qos=1)
                elif mq and now - t_beat >= 1.0:
                    t_beat = now
                    mq.publish("rlgl/gateway/vision", {"event": "heartbeat", "state": state, "player_seen": cur is not None,
                                                       "fps": round(fps, 1), "infer_ms": round(infer_ms, 1)}, qos=0)
                if r.triggered:
                    caught = {"part": r.part, "joint": r.joint, "score": r.score}
                    if rec is not None:
                        rec.mark(now, rid, {"result": "GAME_OVER", "cause": "camera", "part": r.part, "joint": r.joint,
                                            "score": round(r.score, 3)})
                    print(f"  >>> MOVED: {r.part}/{r.joint} score={r.score:.2f}")

                # ----- video
                vis = frame.copy()
                draw_mp_all(vis, pose.all_points)
                draw_overlay(vis, state, det, r, cur, caught, fps, infer_ms, bool(mq and mq.connected) or a.sim)
                if r.triggered:
                    cv2.imwrite(os.path.join("captures", f"moved_r{rid}_{int(time.time())}.jpg"), vis)
                ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, a.quality])
                if ok and rec is not None:
                    rec.add(now, buf.tobytes(), {"state": state, "score": None if r.score is None else round(r.score, 3),
                                                  "part": r.part, "joint": r.joint, "moved": r.moved})
                    rec.poll(now)
                with self.lock:
                    if ok:
                        self.jpg, self.jseq = buf.tobytes(), self.jseq + 1
                    self.status = {"state": state, "round_id": rid, "fps": round(fps, 1), "infer_ms": round(infer_ms, 1),
                                   "mqtt": bool(mq and mq.connected), "sim": a.sim, "detector": r.to_dict(),
                                   "caught": caught, "last_replay": rec.last if rec else None}
                    self.cond.notify_all()
        finally:
            cam.close()
            pose.landmarker.close()
            if mq:
                mq.close()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Pi client · RLGL</title>
<style>body{margin:0;background:#0E0F1F;color:#F4F1E8;font-family:system-ui,sans-serif;display:flex;gap:16px;padding:16px;flex-wrap:wrap}
img{width:720px;max-width:100%;border:4px solid #05060F}pre{background:#0A0B17;padding:12px;font-size:12px;max-height:540px;overflow:auto;width:420px;white-space:pre-wrap}
h3{margin:0 0 8px;font-size:14px;color:#8F93C9}.err{color:#FF3B4E}</style></head><body>
<div><img src="/stream"><p style="color:#8F93C9;font-size:13px">ฟ้า = ท่าตั้งต้น · กรอบสี = จุดที่ขยับมากสุด (เขียว &lt; 0.6, เหลือง &lt; 1, แดง = เกินเกณฑ์)</p></div>
<div><h3>LOCAL STATUS (/status)</h3><pre id="st"></pre>
<h3>MQTT rlgl/# (ws://<span id="ws"></span>)</h3><pre id="log">connecting…</pre></div>
<script src="https://unpkg.com/mqtt@5/dist/mqtt.min.js"></script>
<script>
async function poll(){try{document.getElementById('st').textContent=JSON.stringify(await (await fetch('/status')).json(),null,1)}catch(e){}setTimeout(poll,500)}poll();
const q=new URLSearchParams(location.search);const ws=q.get('ws')||location.hostname+':9001';document.getElementById('ws').textContent=ws;
const log=document.getElementById('log');const lines=[];
try{const c=mqtt.connect('ws://'+ws);c.on('connect',()=>{log.textContent='connected';c.subscribe('rlgl/#')});
c.on('error',e=>{log.innerHTML='<span class=err>MQTT websocket error: '+e.message+'</span>'});
c.on('message',(t,m)=>{let s=m.toString();if(s.length>220)s=s.slice(0,220)+'…';lines.unshift(t+'  '+s);lines.length=Math.min(lines.length,60);log.textContent=lines.join('\\n')})}
catch(e){log.innerHTML='<span class=err>mqtt.js not loaded (no internet?)</span>'}
</script></body></html>"""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = urlparse(self.path).path
            if p == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif p == "/status":
                with app.lock:
                    s = json.dumps(app.status, ensure_ascii=False, default=str).encode()
                self._send(200, "application/json", s)
            elif p == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with app.cond:
                            app.cond.wait_for(lambda: app.jseq != last, timeout=2.0)
                            jpg, last = app.jpg, app.jseq
                        if jpg:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode()
                                             + b"\r\n\r\n" + jpg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="picam", help='"picam" = กล้อง CSI บน Pi, ตัวเลข = เว็บแคม (laptop ใช้ 0)')
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--variant", choices=["lite", "full", "heavy"], default="lite")
    ap.add_argument("--k", type=float, default=4.5, help="ความไวของตัวตรวจการขยับ")
    ap.add_argument("--broker", default=os.environ.get("MQTT_HOST", "localhost"), help="IP ของ broker (laptop)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--device-id", default="pi-01")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--sim", action="store_true", help="สลับไฟเองในเครื่อง ไม่ใช้ MQTT")
    ap.add_argument("--calib", type=float, default=5.0, help="(--sim) วินาทีที่ให้ยืนนิ่งตอนเริ่ม")
    ap.add_argument("--replay-url", default=None, help="ที่อยู่ replay_server (ค่าเริ่มต้น http://<broker>:5000/api/replay)")
    ap.add_argument("--pre", type=float, default=8.0, help="คลิป: วินาทีก่อนเหตุการณ์")
    ap.add_argument("--post", type=float, default=2.0, help="คลิป: วินาทีหลังเหตุการณ์")
    ap.add_argument("--no-replay", action="store_true", help="ไม่ต้องทำคลิป")
    args = ap.parse_args()
    if args.replay_url is None and not args.sim:
        args.replay_url = f"http://{args.broker}:5000/api/replay"

    app = App(args)
    server = None
    for port in [args.http_port] + [p for p in (8080, 8081, 8888, 5000, 5050, 9090) if p != args.http_port]:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
            break
        except OSError as e:  # Windows: port ถูกใช้หรือถูกจองไว้ (WinError 10013 / 10048)
            print(f"port {port} ใช้ไม่ได้ ({e.__class__.__name__}) ลอง port ถัดไป")
    if server is None:
        raise SystemExit("หา port ว่างไม่เจอ ลองระบุเองด้วย --http-port 8123")
    server.daemon_threads = True
    worker = threading.Thread(target=app.run, daemon=True)
    worker.start()
    print(f"ภาพสด: http://localhost:{server.server_address[1]}   (Ctrl+C เพื่อหยุด)")
    try:
        while worker.is_alive():
            server.timeout = 0.5
            server.handle_request()
    except KeyboardInterrupt:
        pass
    app.running = False
    worker.join(timeout=3)
    server.server_close()


if __name__ == "__main__":
    main()
