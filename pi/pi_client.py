"""
pi_client.py: งานฝั่ง Pi (gateway) สำหรับโหมด RLGL
    กล้อง → MediaPipe Pose → motion_detector → MQTT  rlgl/gateway/vision
                                             → HTTP  ภาพสด MJPEG (มีโครงร่าง + จุดที่ขยับ)
    และฟัง rlgl/game/state จาก Game Engine เพื่อรู้ว่าเมื่อไหร่ calibrate / เริ่มจับไฟแดง

บน Pi จริง (ในโฟลเดอร์ pi/):  python3 pi_client.py --backend http://<IP laptop>:8000
    broker อยู่บน Pi เอง (--broker ค่าเริ่มต้น localhost), กล้อง CSI เป็นค่าเริ่มต้น
    ภาพสด: http://<IP Pi>:8000/stream   สถานะ: http://<IP Pi>:8000/status
    state ของเกมมาจาก game engine บน laptop (laptop/engine)

ทดสอบบน laptop ด้วยเว็บแคม: python pi_client.py --source 0 --broker <IP Pi>
ไม่มี broker ก็ลองได้:        python pi_client.py --source 0 --sim   (สลับไฟเองในเครื่อง ไม่ส่ง MQTT)

ข้อความที่ส่ง (ตาม docs/mqtt-contract.md: JSON ชั้นเดียว, จริง/เท็จ = 1/0, ทุกข้อความมี device_id, seq, ts, round_id):
    rlgl/gateway/vision  QoS 0  ทุกเฟรมช่วง RED_LIGHT: motion_score, confidence, fps, inference_ms
                                (+ moved, part, joint, joint_x, joint_y, valid, n_joints)
                         QoS 1  event="moved" ตอนจับได้ครั้งแรกของรอบ / event="calibrated" ตอน calibrate เสร็จ
                         QoS 0  นอกไฟแดงส่งสรุป fps ทุก 1 วินาที (event="heartbeat")
    rlgl/gateway/replay  QoS 1  ส่งคลิปของรอบสำเร็จแล้ว: round_id, video_url, moved_at (วินาทีในคลิปที่กล้องจับได้ว่าขยับ, null = ไม่ได้แพ้เพราะกล้อง),
                                ended_at (วินาทีในคลิปที่เกมจบ), duration, result, reason, part, joint, motion_score,
                                clip_started_at / moved_ts (epoch ms)  → หน้าเว็บตั้ง video.currentTime = moved_at ได้เลย
    rlgl/status/<device_id>  QoS 1 retain  status = online / offline (offline ส่งโดย broker อัตโนมัติถ้าหลุด = LWT)
รับ:
    rlgl/game/state      {"state": "CALIBRATE" | "GREEN_LIGHT" | "TRANSITION" | "RED_LIGHT" | "WIN" | "GAME_OVER" | "IDLE", "round_id": "r012"}
                         Game Engine ต้องส่ง RED_LIGHT หลังหมดช่วงผ่อนผันแล้ว (Pi เริ่มเก็บท่าตั้งต้นทันทีที่ได้รับ)
    rlgl/game/result     {"result": "GAME_OVER" | "WIN", "reason": "camera" | ..., "round_id": "r012"}

ภาพสด (HTTP บน Pi, ให้หน้าเว็บฝังได้ เช่นตอน calibrate ให้ผู้เล่นเห็นว่ากล้องเห็นตัวไหม):
    GET http://<IP Pi>:8000/stream        MJPEG ต่อเนื่อง (มีโครงร่าง)  ใช้ <img src="http://<IP Pi>:8000/stream">
    GET http://<IP Pi>:8000/snapshot.jpg  ภาพเดียวล่าสุด
    GET http://<IP Pi>:8000/status        JSON: state, player_seen, visible_joints, calib_frames, fps, ...
    และ MQTT rlgl/gateway/vision event="heartbeat" (ทุก 0.5 วิตอน CALIBRATE): player_seen, visible_joints, calib_frames

คลิปรีเพลย์ (HTTP) รอบละ 1 คลิป:
    --clip full  (ค่าเริ่มต้น) อัดทั้งเกมลงไฟล์ระหว่างเล่น พอ GAME_OVER / WIN อัดต่ออีก --post วินาทีแล้วส่ง
    --clip short คลิป --pre วินาทีก่อนขยับ + --post วินาทีหลังขยับ ส่งทันทีที่จับได้
    ไฟล์อยู่ใน captures/replays/ (วิดีโอ + .json สีไฟรายเฟรม / moved_at) แล้วส่งไปที่:
      --backend    backend ของเว็บ: POST <backend>/clips  (round_id, video = MP4 H.264, meta = .json) ตาม docs/mqtt-contract.md
                   backend จะประกาศ rlgl/game/media ให้หน้าเว็บเอง  ส่งไม่ได้ → เก็บไฟล์ไว้และลองใหม่เรื่อยๆ
      --replay-url replay_server.py (ตัวทดสอบ มีหน้าดู timeline) แล้วแจ้ง rlgl/gateway/replay
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import queue
from collections import deque
import threading
import time
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
    """picam = กล้อง CSI บน Pi (picamera2), ตัวเลข = เว็บแคม/USB, ชื่อไฟล์ = เล่นวิดีโอวนซ้ำแทนกล้อง (ไว้ทดสอบ)"""

    def __init__(self, source, w, h):
        self.picam = self.cap = None
        self.file, self.fps, self.t_start, self.n = False, 15.0, None, 0
        if source != "picam" and not str(source).isdigit():
            if not os.path.exists(source):
                raise RuntimeError(f"ไม่พบไฟล์วิดีโอ {source}")
            self.cap = cv2.VideoCapture(source)
            if not self.cap.isOpened():
                raise RuntimeError(f"เปิดไฟล์วิดีโอ {source} ไม่ได้")
            self.file = True
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
            # ไฟล์ .webm บางไฟล์บอก fps ผิด (เช่น 1000) → ใช้เวลาของแต่ละเฟรมในไฟล์แทน ถ้าไม่มีค่อยใช้ fps
            self.fps = fps if 1 <= fps <= 60 else 15.0
            print(f"ใช้ไฟล์วิดีโอแทนกล้อง: {source} (ไฟล์บอก {fps:.1f} fps, เล่นตามเวลาในไฟล์ วนซ้ำ)")
            return
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
        if self.file:
            return self._read_file()
        ok, f = self.cap.read()
        return f if ok else None

    def _read_file(self):
        """เล่นไฟล์ให้เหมือนกล้องจริง: ภาพที่ได้ตรงกับเวลาจริงเสมอ
        ประมวลผลไม่ทัน → ข้ามเฟรม (กล้องจริงก็ทิ้งเฟรม), เร็วเกิน → รอ  ไม่อย่างนั้นคลิปที่อัดจะเร็วหรือช้ากว่าจริง"""
        if self.t_start is None:
            self.t_start = time.perf_counter()
        elapsed = time.perf_counter() - self.t_start
        f = None
        while True:
            ok, fr = self.cap.read()
            if not ok:  # จบไฟล์ → วนกลับต้นไฟล์
                if f is not None:
                    return f
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.t_start, self.n, elapsed = time.perf_counter(), 0, 0.0
                ok, fr = self.cap.read()
                if not ok:
                    return None
            self.n += 1
            ts = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if ts <= 0 and self.n > 1:
                ts = (self.n - 1) / self.fps
            f = fr
            if ts >= elapsed - 0.5 / self.fps:  # เฟรมนี้ถึงเวลาแล้ว (หรือยังไม่ถึง → รอ)
                wait = ts - elapsed
                if wait > 0:
                    time.sleep(wait)
                return f

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
        self.status_topic = f"rlgl/status/{self.dev}"
        if args.mqtt_user:
            self.cli.username_pw_set(args.mqtt_user, args.mqtt_pass or None)
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
    """--sim: เป็น Game Engine จำลองในเครื่อง (ไม่มี broker)
    CALIBRATE → (เขียว → เหลือง → แดง) วนไป  ขยับตอนไฟแดง = GAME_OVER, รอดไฟแดงครบ win_after ครั้ง = WIN
    จบแล้วพัก `over` วินาที แล้วเริ่มเกมใหม่ (round_id + 1)"""
    DUR = {"GREEN_LIGHT": 4.0, "TRANSITION": 1.5, "RED_LIGHT": 4.0}
    NEXT = {"CALIBRATE": "GREEN_LIGHT", "GREEN_LIGHT": "TRANSITION", "TRANSITION": "RED_LIGHT"}

    def __init__(self, calib, win_after=3, over=4.0):
        self.calib, self.win_after, self.over = calib, win_after, over
        self.state, self.t_end, self.n, self.survived = None, 0.0, 1, 0
        self.stamp = time.strftime("%H%M%S")
        self.changed, self.result = False, None

    def _set(self, st, dur):
        self.state, self.t_end, self.changed = st, time.perf_counter() + dur, True

    def caught(self, info):
        if self.state == "RED_LIGHT":
            self.result = {"result": "GAME_OVER", "cause": "camera", **info}
            self._set("GAME_OVER", self.over)

    def take_result(self):
        r, self.result = self.result, None
        return r

    def take_state(self):
        now = time.perf_counter()
        if self.state is None:
            self._set("CALIBRATE", self.calib)
        elif now >= self.t_end:
            if self.state in self.NEXT:
                nxt = self.NEXT[self.state]
                self._set(nxt, self.DUR[nxt])
            elif self.state == "RED_LIGHT":
                self.survived += 1
                if self.win_after and self.survived >= self.win_after:
                    self.result = {"result": "WIN", "cause": "finish", "red_lights": self.survived}
                    self._set("WIN", self.over)
                else:
                    self._set("GREEN_LIGHT", self.DUR["GREEN_LIGHT"])
            else:  # GAME_OVER / WIN → เกมใหม่
                self.n, self.survived = self.n + 1, 0
                self._set("GREEN_LIGHT", self.DUR["GREEN_LIGHT"])
        if not self.changed:
            return None
        self.changed = False
        return {"state": self.state, "round_id": f"sim{self.stamp}_{self.n}"}


# ---------------------------------------------------------------- replay
def safe_round_id(rid):
    """round_id ใช้ได้เฉพาะ A-Z a-z 0-9 _ - (ตามข้อตกลง)"""
    if rid is None or str(rid) == "":
        rid = "local_" + time.strftime("%Y%m%d_%H%M%S")
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(rid))[:64]


def epoch_ms(t_perf):
    """แปลงเวลา time.perf_counter() เป็น epoch มิลลิวินาที (แบบเดียวกับ ts ใน MQTT)"""
    return int((time.time() - (time.perf_counter() - t_perf)) * 1000)


def clip_info(meta, video, codec):
    """ข้อมูลสรุปของคลิป (JSON ชั้นเดียว) ใช้ทั้งใน rlgl/gateway/replay และสถานะ"""
    return {"round_id": safe_round_id(meta["round_id"]), "file": os.path.basename(video), "codec": codec,
            "result": meta.get("result"), "reason": meta.get("cause"),
            "moved_at": meta.get("camera_moved_at"), "ended_at": meta.get("ended_at"), "duration": meta.get("duration"),
            "part": meta.get("part"), "joint": meta.get("joint"), "motion_score": meta.get("score"),
            "clip_started_at": meta.get("clip_started_at"), "moved_ts": meta.get("moved_ts")}


def post_clip(url, round_id, video_path, json_path=None, token=None):
    """POST แบบ multipart: round_id + video (+ meta ถ้ามี)"""
    import requests
    mime = "video/mp4" if video_path.endswith(".mp4") else "video/webm"
    with open(video_path, "rb") as v:
        files = {"video": (os.path.basename(video_path), v, mime)}
        m = open(json_path, "rb") if json_path else None
        try:
            if m:
                files["meta"] = (os.path.basename(json_path), m, "application/json")
            r = requests.post(url, data={"round_id": str(round_id)}, files=files, timeout=(5, 120),
                              headers={"X-Upload-Token": token} if token else {})
        finally:
            if m:
                m.close()
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


class Uploader:
    """ส่งคลิปออกจาก Pi (เธรดแยก ไม่ทำให้กล้องช้า)
    backend   : POST <backend>/clips ตาม docs/mqtt-contract.md (ต้องเป็น MP4 H.264)
    replay_url: replay_server.py สำหรับทดสอบ
    ส่งไม่สำเร็จ → เก็บไฟล์ไว้ ลองใหม่ทุก 5, 10, 15 … (สูงสุด 60) วินาที จนกว่าจะได้"""

    def __init__(self, args, link):
        self.backend = args.backend.rstrip("/") + "/clips" if args.backend else None
        self.token, self.replay_url, self.link = args.upload_token, args.replay_url, link
        self.q, self.last, self.pending = queue.Queue(), None, 0
        if not (self.backend or self.replay_url):
            print("คำเตือน: ไม่ได้ใส่ --backend หรือ --replay-url คลิปจะถูกเก็บไว้ใน captures/replays อย่างเดียว")
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, video, json_path, info):
        todo = []
        if self.backend:
            if video.endswith(".mp4") and info.get("codec") in ("h264", "avc1"):
                todo.append("backend")
            else:
                print(f"** ไม่ส่งเข้า backend: backend รับเฉพาะ MP4 H.264 แต่คลิปนี้เป็น {info.get('codec')} "
                      "ติดตั้ง ffmpeg บน Pi (sudo apt install ffmpeg)")
        if self.replay_url:
            todo.append("replay")
        self.last = {**info, "uploaded": False, "todo": list(todo)}
        if todo:
            self.q.put({"video": video, "json": json_path, "info": info, "todo": todo, "targets": list(todo),
                        "tries": 0, "next": 0.0})

    def _loop(self):
        jobs = []
        while True:
            try:
                jobs.append(self.q.get(timeout=1.0))
            except queue.Empty:
                pass
            self.pending = len(jobs)
            for job in list(jobs):
                if time.time() < job["next"]:
                    continue
                self._try(job)
                if not job["todo"]:
                    jobs.remove(job)
                else:
                    job["tries"] += 1
                    job["next"] = time.time() + min(60, 5 * job["tries"])
                    print(f"  จะลองส่ง {os.path.basename(job['video'])} ใหม่ในอีก {job['next'] - time.time():.0f} วินาที "
                          f"(ไฟล์อยู่ใน captures/replays)")

    def _try(self, job):
        info, rid = job["info"], job["info"]["round_id"]
        for target in list(job["todo"]):
            try:
                if target == "backend":
                    res = post_clip(self.backend, rid, job["video"], job["json"], token=self.token)
                    info["video_url"] = res.get("url")
                    print(f"ส่งคลิปเข้า backend แล้ว: {info['video_url']}")
                else:
                    res = post_clip(self.replay_url, rid, job["video"], job["json"])
                    info.update(url=urljoin(self.replay_url, res.get("url", "")),
                                page=urljoin(self.replay_url, res.get("page", "")))
                    print("replay uploaded:", info["page"])
                job["todo"].remove(target)
            except Exception as e:
                name = "backend" if target == "backend" else "replay_server"
                print(f"ส่งคลิปไป {name} ไม่สำเร็จ: {str(e)[:200]}")
        self.last = {**info, "uploaded": not job["todo"], "todo": list(job["todo"])}
        main = "backend" if self.backend and "backend" in job["targets"] else "replay"
        if main not in job["todo"] and not job.get("announced") and self.link is not None:
            # ส่งคลิปเข้าที่หลักสำเร็จ → บอกหน้าเว็บว่าคลิปพร้อม + ขยับวินาทีที่เท่าไร (ไม่รอ replay_server ที่เป็นตัวเสริม)
            job["announced"] = True
            msg = {k: v for k, v in info.items() if k not in ("file", "codec")}
            self.link.publish("rlgl/gateway/replay", msg, qos=1)
            print(f"ประกาศ rlgl/gateway/replay: moved_at={info.get('moved_at')} s, ended_at={info.get('ended_at')} s")


class VideoSink:
    """เขียนวิดีโอทีละเฟรม: ffmpeg → H.264 .mp4 (ดีที่สุด), ไม่มี ffmpeg → OpenCV VP8 .webm"""

    def __init__(self, base, w, h, fps):
        self.proc = self.vw = None
        ff = shutil.which("ffmpeg")
        if ff:
            self.path, self.codec = base + ".mp4", "h264"
            cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:g}",
                   "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", self.path]
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                return
            except OSError as e:
                print("เปิด ffmpeg ไม่ได้:", e)
        for fourcc, ext in (("VP80", "webm"), ("avc1", "mp4"), ("mp4v", "mp4")):
            path = f"{base}.{ext}"
            vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
            if vw.isOpened():
                self.vw, self.path, self.codec = vw, path, fourcc.lower()
                if fourcc == "mp4v":
                    print("คำเตือน: ได้คลิป mp4v ซึ่งเบราว์เซอร์มักเล่นไม่ได้ ติดตั้ง ffmpeg เพื่อให้ได้ H.264")
                return
            vw.release()
        raise RuntimeError("ไม่มีตัวเขียนวิดีโอที่ใช้ได้ (ติดตั้ง ffmpeg: sudo apt install ffmpeg)")

    def write(self, img):
        if self.proc:
            self.proc.stdin.write(img.tobytes())
        else:
            self.vw.write(img)

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            err = self.proc.stderr.read().decode(errors="ignore")
            if self.proc.wait(timeout=120) != 0:
                print("ffmpeg error:", err[-300:])
        else:
            self.vw.release()
        return os.path.exists(self.path) and os.path.getsize(self.path) > 1000


class RoundTake:
    """การอัด 1 เกม: เธรดแยกเขียนวิดีโอ เวลาในคลิปตรงกับเวลาจริง (เฟรมไม่ทันก็ซ้ำเฟรมเดิม)"""

    def __init__(self, rec, t0, round_id):
        self.rec, self.t0, self.round_id = rec, t0, round_id
        self.q = queue.Queue(maxsize=60)
        self.frames, self.event, self.end, self.stop_at = [], None, None, None
        self.dropped = 0
        self.base = os.path.join(rec.dir, f"{round_id}_{time.strftime('%Y%m%d_%H%M%S')}")
        self.th = threading.Thread(target=self._writer, daemon=True)
        self.th.start()

    def add(self, t, img, meta):
        try:
            self.q.put_nowait((t, img))
        except queue.Full:
            self.dropped += 1
        self.frames.append({"t": round(t - self.t0, 3), **meta})

    def _writer(self):
        sink, written, fps = None, 0, self.rec.fps
        self.video = self.codec = None
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                t, img = item
                if sink is None:
                    h, w = img.shape[:2]
                    sink = VideoSink(self.base, w, h, fps)
                target = int((t - self.t0) * fps) + 1  # เฟรมที่ควรมีถึงเวลานี้
                while written < target:
                    sink.write(img)
                    written += 1
            if sink and sink.close():
                self.video, self.codec = sink.path, sink.codec
        except Exception as e:
            print("เขียนวิดีโอไม่สำเร็จ:", e)
        self.n_written = written
        self.rec._finished(self)


class RoundRecorder:
    """อัดทั้งเกมลงไฟล์ระหว่างเล่น แล้วส่งไป laptop ครั้งเดียวตอนจบ (GAME_OVER / WIN)"""

    def __init__(self, args, uploader):
        self.fps, self.width, self.post, self.max_len = args.rec_fps, args.rec_width, args.post, args.max_len
        self.uploader = uploader
        self.take = None
        self.dir = os.path.join("captures", "replays")
        os.makedirs(self.dir, exist_ok=True)

    @property
    def active(self):
        return self.take is not None

    def start(self, t, round_id):
        round_id = safe_round_id(round_id)
        if self.take and self.take.round_id == round_id:
            return
        if self.take:  # เกมเปลี่ยนโดยไม่มีผลจบ → ปิดคลิปเก่าก่อน
            self.finish(t, {"result": None, "cause": "interrupted"}, post=0)
            self.poll(t)
        self.take = RoundTake(self, t, round_id)
        print(f"เริ่มอัดเกม round {round_id}")

    def moved(self, t, info):
        if self.take and self.take.event is None:
            self.take.event = {"t": t, **info}

    def finish(self, t, info, post=None):
        if self.take and self.take.end is None:
            self.take.end = {"t": t, **info}
            self.take.stop_at = t + (self.post if post is None else post)

    def add(self, t, img, meta):
        if not self.take:
            return
        h, w = img.shape[:2]
        if self.width and w > self.width:
            nh = int(round(h * self.width / w / 2)) * 2
            img = cv2.resize(img, (self.width // 2 * 2, nh), interpolation=cv2.INTER_AREA)
        self.take.add(t, img, meta)

    def poll(self, t):
        tk = self.take
        if tk is None:
            return
        if tk.stop_at is None and t - tk.t0 > self.max_len:
            print(f"เกมยาวเกิน {self.max_len:.0f} วินาที ตัดคลิปเลย")
            self.finish(t, {"result": None, "cause": "max_len"}, post=0)
        if tk.stop_at is not None and t >= tk.stop_at:
            self.take = None
            tk.q.put(None)  # ให้เธรดเขียนไฟล์ปิดคลิปแล้วอัปโหลด

    def close(self):
        tk = self.take
        if tk:
            self.finish(time.perf_counter(), {"result": None, "cause": "stopped"}, post=0)
            self.poll(time.perf_counter())
            tk.th.join(timeout=60)

    def _finished(self, tk):
        if not tk.video:
            print("ทำคลิปไม่สำเร็จ")
            return
        fps = self.fps
        dur = tk.n_written / fps
        end, ev = tk.end or {}, tk.event or {}
        mark = ev.get("t", end.get("t"))
        res = {k: v for k, v in end.items() if k != "t"}
        meta = {"kind": "full_round", "round_id": tk.round_id, "result": res.get("result"), "cause": res.get("cause"),
                "part": ev.get("part", res.get("part")), "joint": ev.get("joint", res.get("joint")),
                "score": ev.get("score", res.get("score")),
                "moved_at": None if mark is None else round(min(mark - tk.t0, dur), 3),
                "camera_moved_at": None if "t" not in ev else round(min(ev["t"] - tk.t0, dur), 3),
                "moved_ts": None if "t" not in ev else epoch_ms(ev["t"]),
                "ended_at": None if end.get("t") is None else round(min(end["t"] - tk.t0, dur), 3),
                "clip_started_at": epoch_ms(tk.t0),
                "duration": round(dur, 3), "fps": fps, "video": os.path.basename(tk.video), "codec": tk.codec,
                "dropped_frames": tk.dropped, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "frames": [f for f in tk.frames if f["t"] <= dur]}
        with open(tk.base + ".json", "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False)
        size = os.path.getsize(tk.video) / 1e6
        print(f"replay saved: {tk.video} ({dur:.1f} s, {size:.1f} MB, event at {meta['moved_at']} s)")
        self.uploader.submit(tk.video, tk.base + ".json", clip_info(meta, tk.video, tk.codec))


# ---------------------------------------------------------------- short clip (--clip short)
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


class ReplayRecorder:
    """เก็บเฟรมย้อนหลังในหน่วยความจำ แล้วตัดคลิปรอบละ 1 คลิปเมื่อมีเหตุการณ์ (ขยับ / GAME_OVER / WIN)"""

    def __init__(self, args, uploader):
        self.pre, self.post, self.uploader = args.pre, args.post, uploader
        self.buf = deque()
        self.pending, self.done = None, set()
        self.dir = os.path.join("captures", "replays")
        os.makedirs(self.dir, exist_ok=True)

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
        rid = safe_round_id(p["round_id"])
        base = os.path.join(self.dir, f"{rid}_{time.strftime('%Y%m%d_%H%M%S')}")
        video, codec = write_video([f[1] for f in frames], fps, base)
        if not video:
            print("ทำคลิปไม่สำเร็จ")
            return
        meta = {"round_id": p["round_id"], "result": p.get("result"), "cause": p.get("cause"), "part": p.get("part"),
                "joint": p.get("joint"), "score": p.get("score"), "moved_at": round(p["t"] - t0, 3),
                "camera_moved_at": round(p["t"] - t0, 3) if p.get("cause") == "camera" else None,
                "moved_ts": epoch_ms(p["t"]) if p.get("cause") == "camera" else None,
                "ended_at": None if p.get("cause") == "camera" else round(p["t"] - t0, 3),
                "clip_started_at": epoch_ms(t0), "duration": round(t1 - t0, 3), "fps": round(fps, 2), "video": os.path.basename(video), "codec": codec,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "frames": [{"t": round(f[0] - t0, 3), **f[2]} for f in frames]}
        with open(base + ".json", "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False)
        print(f"replay saved: {video} ({len(frames)} frames, {fps:.1f} fps, event at {meta['moved_at']} s)")
        self.uploader.submit(video, base + ".json", clip_info(meta, video, codec))

    # ให้เรียกแบบเดียวกับ RoundRecorder ได้
    active = False

    def start(self, t, round_id):
        pass

    def moved(self, t, info):
        self.mark(t, safe_round_id(info.pop("round_id", None)), {"result": "GAME_OVER", "cause": "camera", **info})

    def finish(self, t, info, post=None):
        if info.get("result") in ("GAME_OVER", "WIN"):
            self.mark(t, safe_round_id(info.pop("round_id", None)), info)

    def add(self, t, img, meta):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.buf.append((t, buf.tobytes(), meta))
            while self.buf and t - self.buf[0][0] > self.pre + self.post + 2:
                self.buf.popleft()

    def close(self):
        pass


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
        link = SimGame(a.calib, a.sim_win) if a.sim else Link(a)
        mq = None if a.sim else link
        det = MotionDetector(DetectorConfig(k_sigma=a.k))
        up = Uploader(a, mq)
        rec = None if a.no_replay else (ReplayRecorder(a, up) if a.clip == "short" else RoundRecorder(a, up))
        cam = Camera(a.source, a.width, a.height)
        pose = MediaPipePose(a.variant)
        state, rid, caught, r = None, None, None, None
        fps = infer_ms = 0.0
        t_prev = t_beat = time.perf_counter()
        t_last_frame = t_warn = time.perf_counter()
        os.makedirs("captures", exist_ok=True)
        print(f"เปิดกล้อง/วิดีโอแล้ว ({a.source}) รอภาพเฟรมแรก…")
        try:
            while self.running:
                frame = cam.read()
                if frame is None:
                    if time.perf_counter() - t_last_frame > 3 and time.perf_counter() - t_warn > 3:
                        t_warn = time.perf_counter()
                        print(f"** อ่านภาพจากกล้อง '{a.source}' ไม่ได้มา {time.perf_counter() - t_last_frame:.0f} วินาที "
                              "เช็ก: ls /dev/video* / v4l2-ctl --list-devices / ลอง --source 1 หรือ --source <ไฟล์วิดีโอ>")
                    time.sleep(0.05)
                    continue
                t_last_frame = time.perf_counter()
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
                    print(f"state: {state} → {new}  (round {rid})")
                    state = new
                    if rec is not None:
                        if new in ("CALIBRATE", "GREEN_LIGHT", "TRANSITION", "RED_LIGHT"):
                            rec.start(now, rid)  # เริ่มอัดตอนเกมเริ่ม (ถ้าอัดรอบนี้อยู่แล้วไม่ทำอะไร)
                        elif new in ("GAME_OVER", "WIN"):
                            res = link.take_result() or msg
                            rec.finish(now, {"round_id": rid, "result": new,
                                             "cause": res.get("reason") or res.get("cause") or ("finish" if new == "WIN" else None),
                                             "part": res.get("part"), "joint": res.get("joint"), "score": res.get("score")})
                        elif new == "IDLE":
                            rec.finish(now, {"result": None, "cause": "idle"}, post=0)

                r = det.update(now, cur)

                # ----- publish
                visible = 0 if cur is None else int((np.asarray(cur[1]) > 0.5).sum())  # จุดบนตัว (จาก 17) ที่กล้องเห็นชัด
                calib_frames = len(det.calib_buf) if state == "CALIBRATE" else None
                if state == "RED_LIGHT" and mq:
                    d = r.to_dict()
                    data = {"event": "frame", "motion_score": d["score"], "confidence": d["confidence"],
                            "fps": round(fps, 1), "inference_ms": round(infer_ms, 1), "moved": int(bool(d["moved"])),
                            "valid": int(bool(d["valid"])), "part": d["part"], "joint": d["joint"], "n_joints": d["n_joints"]}
                    if r.joint and cur is not None:
                        x, y = cur[0][JOINTS.index(r.joint)]
                        data["joint_x"] = round(float(x) / frame.shape[1], 3)
                        data["joint_y"] = round(float(y) / frame.shape[0], 3)
                    mq.publish("rlgl/gateway/vision", data, qos=0)
                    if r.triggered:
                        mq.publish("rlgl/gateway/vision", {**data, "event": "moved"}, qos=1)
                elif mq and now - t_beat >= (0.5 if state == "CALIBRATE" else 1.0):
                    t_beat = now  # ตอน calibrate ส่งถี่ขึ้น ให้หน้าเว็บบอกผู้เล่นได้ว่ากล้องเห็นตัวไหม
                    mq.publish("rlgl/gateway/vision", {"event": "heartbeat", "state": state, "player_seen": int(cur is not None),
                                                       "visible_joints": visible, "calib_frames": calib_frames,
                                                       "fps": round(fps, 1), "inference_ms": round(infer_ms, 1)}, qos=0)
                if r.triggered:
                    caught = {"part": r.part, "joint": r.joint, "score": r.score}
                    if rec is not None:
                        rec.moved(now, {"round_id": rid, "part": r.part, "joint": r.joint, "score": round(r.score, 3)})
                    if a.sim:
                        link.caught({"part": r.part, "joint": r.joint, "score": round(r.score, 3)})
                    print(f"  >>> MOVED: {r.part}/{r.joint} score={r.score:.2f}")

                # ----- video
                vis = frame.copy()
                draw_mp_all(vis, pose.all_points)
                draw_overlay(vis, state, det, r, cur, caught, fps, infer_ms, bool(mq and mq.connected) or a.sim)
                if r.triggered:
                    cv2.imwrite(os.path.join("captures", f"moved_r{rid}_{int(time.time())}.jpg"), vis)
                ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, a.quality])
                if rec is not None:
                    rec.add(now, vis, {"state": state, "score": None if r.score is None else round(r.score, 3),
                                       "part": r.part, "joint": r.joint, "moved": r.moved})
                    rec.poll(now)
                with self.lock:
                    if ok:
                        self.jpg, self.jseq = buf.tobytes(), self.jseq + 1
                    self.status = {"state": state, "round_id": rid, "player_seen": cur is not None,
                                   "visible_joints": visible, "calib_frames": calib_frames, "fps": round(fps, 1), "infer_ms": round(infer_ms, 1),
                                   "mqtt": bool(mq and mq.connected), "sim": a.sim, "detector": r.to_dict(),
                                   "caught": caught, "recording": bool(rec and rec.active),
                                   "last_clip": up.last, "clips_waiting": up.pending}
                    self.cond.notify_all()
        finally:
            if rec is not None:
                rec.close()
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
            elif p == "/snapshot.jpg":
                with app.lock:
                    jpg = app.jpg
                if jpg:
                    self._send(200, "image/jpeg", jpg)
                else:
                    self._send(503, "text/plain", b"no frame yet")
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
    ap.add_argument("--source", default="picam", help='"picam" = กล้อง CSI บน Pi, ตัวเลข = เว็บแคม (laptop ใช้ 0), ชื่อไฟล์ = วิดีโอทดสอบ')
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--variant", choices=["lite", "full", "heavy"], default="lite")
    ap.add_argument("--k", type=float, default=4.5, help="ความไวของตัวตรวจการขยับ")
    ap.add_argument("--broker", default=os.environ.get("MQTT_HOST", "localhost"), help="IP ของ broker (laptop)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--device-id", default="gateway-01")
    ap.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"), help="บัญชี MQTT (หรือ env MQTT_USER)")
    ap.add_argument("--mqtt-pass", default=os.environ.get("MQTT_PASSWORD"), help="รหัส MQTT (หรือ env MQTT_PASSWORD)")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--sim", action="store_true", help="สลับไฟเองในเครื่อง ไม่ใช้ MQTT")
    ap.add_argument("--calib", type=float, default=5.0, help="(--sim) วินาทีที่ให้ยืนนิ่งตอนเริ่ม")
    ap.add_argument("--backend", default=os.environ.get("RLGL_BACKEND"),
                    help="backend ของเว็บ เช่น http://172.20.10.3:8000 (ส่งคลิปไปที่ /clips)")
    ap.add_argument("--upload-token", default=os.environ.get("RLGL_UPLOAD_TOKEN"), help="ถ้า backend ตั้ง RLGL_UPLOAD_TOKEN")
    ap.add_argument("--replay-url", default=None, help="replay_server.py สำหรับทดสอบ เช่น http://172.20.10.2:5000/api/replay")
    ap.add_argument("--clip", choices=["full", "short"], default="full", help="full = ทั้งเกม, short = รอบจังหวะที่ขยับ")
    ap.add_argument("--pre", type=float, default=8.0, help="(--clip short) วินาทีก่อนขยับ")
    ap.add_argument("--post", type=float, default=2.0, help="คลิป: อัดต่ออีกกี่วินาทีหลังจบเกม / หลังขยับ")
    ap.add_argument("--rec-fps", type=float, default=10.0, help="คลิป: fps ของวิดีโอ")
    ap.add_argument("--rec-width", type=int, default=480, help="คลิป: ย่อภาพให้กว้างเท่านี้ (0 = ไม่ย่อ)")
    ap.add_argument("--max-len", type=float, default=300.0, help="คลิป: ยาวสุดกี่วินาที (กันลืมปิด)")
    ap.add_argument("--sim-win", type=int, default=3, help="(--sim) รอดไฟแดงกี่ครั้งถึงชนะ (0 = ไม่มีชนะ)")
    ap.add_argument("--no-replay", action="store_true", help="ไม่ต้องทำคลิป")
    args = ap.parse_args()

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
    worker.join(timeout=20)  # รอปิดไฟล์คลิปที่อัดค้างอยู่
    server.server_close()


if __name__ == "__main__":
    main()
