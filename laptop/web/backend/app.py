"""Backend ของเกม: รับ MQTT -> ส่งให้หน้าเว็บ, สรุปแต่ละรอบ, รับและให้บริการวิดีโอ,
เก็บชื่อผู้เล่นและผลแต่ละรอบ (SQLite)

รันจากโฟลเดอร์ laptop/web:
    python -m uvicorn backend.app:make_app --factory --host 127.0.0.1 --port 8000
"""
import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import pathlib
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.clips import (ConversionFailed, LocalClips, SupabaseClips, UploadFailed, detect_kind, mjpeg_to_mp4,
                           remove_tree)
from backend.store import DEFAULT_MODE, Store, clean_mode, clean_name
from backend.tracker import MEDIA, MODE, PLAYING, RESULT, STATE, SUMMARY, GameTracker

log = logging.getLogger("uvicorn.error")

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent
LAPTOP_DIR = WEB_DIR.parent
BACKEND_ID, SCREEN_ID = "backend-01", "web-01"
BACKEND_STATUS = f"rlgl/status/{BACKEND_ID}"
SCREEN_STATUS = f"rlgl/status/{SCREEN_ID}"
OWN_TOPICS = {SUMMARY, MEDIA, MODE, BACKEND_STATUS, SCREEN_STATUS}
GameMode = Literal["rlgl", "sniper"]
SERVER_PATHS = ("/api", "/clips", "/dev", "/ws", "/docs", "/redoc", "/openapi")  # ไม่ใช่ไฟล์หน้าเว็บ
ROUND_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
CHUNK = 1024 * 1024
QUEUE_LIMIT = 500
KEEPALIVE_S = 5


@dataclass
class Settings:
    clips_dir: pathlib.Path = WEB_DIR / "clips"
    db_path: pathlib.Path | None = None  # None = เก็บในหน่วยความจำ (ใช้ในเทสต์)
    mqtt_host: str | None = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    upload_token: str | None = None
    max_upload_bytes: int = 300 * CHUNK
    public_base_url: str | None = None
    dev_mode: bool = False
    display_timeout: float = 6.0
    cors_origins: tuple = ()
    web_dir: pathlib.Path | None = None
    stream_url: str | None = None  # ภาพสดจาก Pi (MJPEG ผ่าน HTTP) ที่หน้าเว็บดึงเอง
    grafana_url: str | None = None
    track_cm: int = 300  # ระยะจาก ultrasonic ถึงเส้นเริ่ม ใช้คิดความคืบหน้าบนหน้าเว็บ
    # วิดีโอ: ตั้ง Supabase แล้วจะเก็บใน cloud อย่างเดียว ถ้าไม่ตั้งจะเก็บใน clips_dir
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str = "clips"
    clip_url_ttl_s: int = 3600
    temp_dir: pathlib.Path | None = None  # ที่พักไฟล์ระหว่างแปลง (ลบทุกครั้ง) None = temp ของระบบ
    ffmpeg: str = "ffmpeg"
    isolated: bool = False  # ทดสอบในเครื่องนี้อย่างเดียว: ไม่ต่อ broker ไม่อัป cloud (run.cmd -Sim)

    @property
    def cloud_clips(self):
        return bool(self.supabase_url and self.supabase_key) and not self.isolated

    @classmethod
    def from_env(cls):
        values = {}
        env_file = LAPTOP_DIR / "infra" / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
        values.update(os.environ)
        isolated = values.get("RLGL_ISOLATED") == "1"
        if isolated:  # ไม่สนค่า broker และ Supabase ใน .env
            values["RLGL_MQTT_HOST"] = ""
            values.pop("SUPABASE_URL", None)
            values.pop("SUPABASE_SERVICE_KEY", None)
        return cls(
            isolated=isolated,
            clips_dir=pathlib.Path(values.get("RLGL_CLIPS_DIR") or WEB_DIR / "clips"),
            db_path=pathlib.Path(values.get("RLGL_DB_PATH") or WEB_DIR / "data" / "rlgl.db"),
            mqtt_host=values.get("RLGL_MQTT_HOST", "127.0.0.1") or None,
            mqtt_port=int(values.get("RLGL_MQTT_PORT") or 1883),
            mqtt_user=values.get("MQTT_USER", ""),
            mqtt_password=values.get("MQTT_PASSWORD", ""),
            upload_token=values.get("RLGL_UPLOAD_TOKEN") or None,
            max_upload_bytes=int(values.get("RLGL_MAX_UPLOAD_MB") or 300) * CHUNK,
            public_base_url=values.get("RLGL_PUBLIC_BASE_URL") or None,
            dev_mode=values.get("RLGL_DEV") == "1",
            cors_origins=tuple(o.strip() for o in values.get("RLGL_CORS_ORIGINS", "").split(",") if o.strip()),
            web_dir=WEB_DIR / "frontend",
            stream_url=values.get("RLGL_STREAM_URL") or None,
            grafana_url=values.get("RLGL_GRAFANA_URL") or None,
            track_cm=int(values.get("RLGL_TRACK_CM") or 300),
            supabase_url=values.get("SUPABASE_URL") or None,
            supabase_key=values.get("SUPABASE_SERVICE_KEY") or None,
            supabase_bucket=values.get("SUPABASE_BUCKET") or "clips",
            clip_url_ttl_s=int(values.get("RLGL_CLIP_URL_TTL_S") or 3600),
            ffmpeg=values.get("RLGL_FFMPEG") or "ffmpeg",
        )


def decode_mqtt(topic, raw):
    """คืน dict ถ้าเป็นข้อความที่ควรประมวลผล ไม่เช่นนั้นคืน None"""
    if not topic.startswith("rlgl/") or topic in OWN_TOPICS:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if any(isinstance(v, float) and not math.isfinite(v) for v in payload.values()):
        return None
    return payload


class Hub:
    """ทำงานบน event loop เดียว จึงไม่ต้องใช้ lock"""

    def __init__(self, publisher, store):
        self.publisher = publisher
        self.store = store
        self.player_id = self.player_name = None  # คนที่จะเล่นรอบถัดไป
        self.mode = DEFAULT_MODE  # โหมดที่เลือกจากหน้าเว็บ (game engine อ่านจาก rlgl/game/mode)
        self.saved_round = None
        self.tracker = GameTracker()
        self.queues = set()
        self.displays = 0
        self.seq = {}

    def dispatch(self, topic, payload):
        outcome = self.tracker.handle(topic, payload)
        if outcome.accepted:
            self._broadcast({"type": "message", "topic": topic, "payload": payload})
            try:
                self._record(topic, payload)
            except Exception:  # ฐานข้อมูลมีปัญหาต้องไม่ทำให้จอผู้เล่นหยุดรับ state
                log.exception("could not save %s", topic)
        for out_topic, body in outcome.outgoing:
            self.send(out_topic, body)

    def _record(self, topic, payload):
        if topic == STATE:
            # ถ้า game engine ไม่ส่ง mode มา ใช้โหมดที่เลือกบนหน้าเว็บ
            mode = clean_mode(payload.get("mode")) or self.mode
            self._record_round_start(payload.get("round_id"), payload.get("state"), mode)
        elif topic == RESULT:
            self.store.finish_round(payload)
        elif topic == SUMMARY:
            self.store.save_summary(payload)
        elif topic == MEDIA:
            self.store.set_clip(payload["round_id"], f"{payload['round_id']}.mp4")

    def _record_round_start(self, round_id, state, mode):
        # state เป็น retained: WIN, GAME_OVER, IDLE อาจเป็นของรอบเก่าที่ถูกส่งซ้ำ จึงไม่สร้างรอบจากค่าเหล่านี้
        if round_id == self.saved_round or state not in ("CALIBRATE", *PLAYING):
            return
        created = self.store.start_round(round_id, self.player_id, mode)
        if not created and state == "CALIBRATE":
            existing = self.store.round(round_id)
            if existing and existing["result"]:
                log.warning("round %s already has a result: the game engine must not reuse a round_id", round_id)
        self.saved_round = round_id

    def choose_player(self, player):
        """player = {"id", "name"} หรือ None ถ้าเล่นแบบไม่ใส่ชื่อ"""
        self.player_id = player["id"] if player else None
        self.player_name = player["name"] if player else None
        # ยังไม่เริ่มเล่น (calibrate อยู่) ชื่อใหม่ใช้กับรอบนี้ ถ้าเล่นแล้วใช้กับรอบถัดไป
        if self.tracker.state == "CALIBRATE" and self.saved_round == self.tracker.round_id:
            self.store.assign_player(self.saved_round, self.player_id)

    def send(self, topic, body, device=BACKEND_ID, qos=1, retain=False):
        """ข้อความที่ backend สร้างเอง: ส่งให้ browser และ publish เข้า MQTT"""
        self.seq[device] = self.seq.get(device, 0) + 1
        payload = {"device_id": device, "seq": self.seq[device], "ts": int(time.time() * 1000), **body}
        self.dispatch(topic, payload)
        self.publisher(topic, payload, qos, retain)

    def send_status(self, topic, device, online):
        self.send(topic, {"status": "online" if online else "offline"}, device=device, retain=True)

    def choose_mode(self, mode):
        self.mode = mode
        self.send(MODE, {"mode": mode}, retain=True)

    def announce(self):
        self.send_status(BACKEND_STATUS, BACKEND_ID, True)
        self.send_status(SCREEN_STATUS, SCREEN_ID, self.displays > 0)
        self.send(MODE, {"mode": self.mode}, retain=True)

    def display_changed(self, delta):
        was_online = self.displays > 0
        self.displays += delta
        if (self.displays > 0) != was_online:
            self.send_status(SCREEN_STATUS, SCREEN_ID, self.displays > 0)

    def _broadcast(self, message):
        for queue in self.queues:
            if queue.full():  # browser ที่รับช้าจะเสียข้อความเก่าสุด ไม่ทำให้คนอื่นรอ
                queue.get_nowait()
            queue.put_nowait(message)


class MqttLink:
    def __init__(self, settings):
        import paho.mqtt.client as mqtt

        self.settings = settings
        self.loop = self.hub = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"backend-{uuid.uuid4().hex[:8]}")
        if settings.mqtt_user:
            self.client.username_pw_set(settings.mqtt_user, settings.mqtt_password)
        # ถ้า backend หลุด broker จะประกาศ offline ให้ (game engine ต้องถือว่าจอผู้เล่นใช้ไม่ได้ด้วย)
        self.client.will_set(BACKEND_STATUS, json.dumps({"device_id": BACKEND_ID, "status": "offline"}),
                             qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @property
    def connected(self):
        return self.client.is_connected()

    def publish(self, topic, payload, qos, retain):
        self.client.publish(topic, json.dumps(payload), qos=qos, retain=retain)

    def start(self, loop, hub):
        self.loop, self.hub = loop, hub
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=KEEPALIVE_S)
        self.client.loop_start()

    def stop(self):
        deadline = time.time() + 2
        while self.client.is_connected() and self.client.want_write() and time.time() < deadline:
            time.sleep(0.05)
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT refused the connection: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe("rlgl/#", qos=1)
        self.loop.call_soon_threadsafe(self.hub.announce)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, message):
        payload = decode_mqtt(message.topic, message.payload)
        if payload is not None:
            self.loop.call_soon_threadsafe(self.hub.dispatch, message.topic, payload)


class PlayerName(BaseModel):
    name: str


class ModeChoice(BaseModel):
    mode: GameMode


class DevMessage(BaseModel):
    topic: str
    payload: dict


def make_clips(settings):
    if settings.cloud_clips:
        return SupabaseClips(settings.supabase_url, settings.supabase_key, settings.supabase_bucket,
                             ttl_s=settings.clip_url_ttl_s)
    return LocalClips(settings.clips_dir)


def create_app(settings, publisher=None, clips=None):
    clips = clips or make_clips(settings)
    if not clips.cloud and shutil.which(settings.ffmpeg) is None:
        log.warning("ffmpeg not found: clips sent as JPEG frames cannot be converted")
    store = Store(settings.db_path)
    link = MqttLink(settings) if publisher is None and settings.mqtt_host else None
    hub = Hub(publisher or (link.publish if link else lambda *args: None), store)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if link:
            link.start(asyncio.get_running_loop(), hub)
        hub.announce()
        try:
            yield
        finally:
            hub.send_status(SCREEN_STATUS, SCREEN_ID, False)
            hub.send_status(BACKEND_STATUS, BACKEND_ID, False)
            if link:
                await run_in_threadpool(link.stop)
            store.close()

    app = FastAPI(title="Red Light / Green Light backend", lifespan=lifespan)
    app.state.store = store
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])

    @app.middleware("http")
    async def reject_huge_uploads(request, call_next):
        # ปฏิเสธก่อนรับไฟล์ทั้งก้อน (เผื่อ 64 KB ให้ส่วนหัวของ multipart)
        length = request.headers.get("content-length", "")
        if request.url.path == "/clips" and length.isdigit() and int(length) > settings.max_upload_bytes + 65536:
            return JSONResponse({"detail": "file too large"}, status_code=413)
        response = await call_next(request)
        # ไฟล์หน้าเว็บ: ให้ browser ถามก่อนใช้ของที่เก็บไว้ จอผู้เล่นจะได้โค้ดใหม่เสมอหลังแก้
        if not request.url.path.startswith(SERVER_PATHS):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def clip_url(request, round_id):
        base = settings.public_base_url or str(request.base_url)
        return f"{base.rstrip('/')}/clips/{round_id}.mp4"

    @app.post("/clips")
    async def receive_clip(request: Request, round_id: str = Form(...), video: UploadFile = File(...),
                           fps: float | None = Form(None)):
        """Pi ส่งวิดีโอทั้งรอบ: JPEG ต่อกัน (MJPEG) พร้อม fps ที่อัดได้จริง หรือ MP4 แบบ H.264
        ตอบ 200 เมื่อเก็บสำเร็จแล้วเท่านั้น ถ้าได้คำตอบอื่น Pi ต้องเก็บไฟล์ไว้และส่งใหม่"""
        if settings.upload_token and not hmac.compare_digest(
                request.headers.get("x-upload-token", "").encode(), settings.upload_token.encode()):
            raise HTTPException(401, "missing or wrong X-Upload-Token")
        if not ROUND_ID.fullmatch(round_id):
            raise HTTPException(422, "round_id may only use A-Z a-z 0-9 _ - (max 64)")
        if fps is not None and not (math.isfinite(fps) and 1 <= fps <= 60):
            raise HTTPException(422, "fps must be between 1 and 60")

        if settings.temp_dir:
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
        work = pathlib.Path(tempfile.mkdtemp(prefix="rlgl-clip-", dir=settings.temp_dir))
        try:  # ไม่เก็บสำเนาไว้ในเครื่อง: ลบโฟลเดอร์พักทุกกรณี
            received, kind, size = work / "received", None, 0
            with received.open("wb") as out:
                while chunk := await video.read(CHUNK):
                    if size == 0:
                        kind = detect_kind(chunk)
                        if kind is None:
                            raise HTTPException(415, "send JPEG frames (MJPEG) or an MP4 file")
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "file too large")
                    # เขียนดิสก์ใน thread แยก เพื่อไม่ให้จอผู้เล่นได้รับ state ช้า
                    await run_in_threadpool(out.write, chunk)
            if size == 0:
                raise HTTPException(415, "empty file")
            mp4 = received
            if kind == "mjpeg":
                mp4 = work / f"{round_id}.mp4"
                await run_in_threadpool(mjpeg_to_mp4, received, mp4, fps or 15, settings.ffmpeg)
            await run_in_threadpool(clips.save, mp4, round_id)
        except ConversionFailed as error:
            log.warning("clip %s could not be converted: %s", round_id, error)
            raise HTTPException(422, "the frames could not be turned into a video")
        except UploadFailed as error:
            log.warning("clip %s not stored yet: %s", round_id, error)
            raise HTTPException(503, "could not store the clip now: keep it and try again")
        except RuntimeError as error:
            log.error("%s", error)
            raise HTTPException(503, str(error))
        finally:
            await run_in_threadpool(remove_tree, work)

        # ลิงก์ของ laptop ไม่หมดอายุและไม่มี token ของ cloud (ส่งต่อทาง MQTT และเก็บใน InfluxDB ได้)
        url = clip_url(request, round_id)
        hub.send(MEDIA, {"round_id": round_id, "status": "ready", "video_url": url})
        return {"url": url}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mqtt_connected": bool(link and link.connected), "displays": hub.displays,
                "isolated": settings.isolated}

    @app.get("/api/state")
    async def state():
        return {"messages": hub.tracker.snapshot()}

    @app.get("/api/clips")
    async def clip_list(request: Request):
        if clips.cloud:
            return {"clips": [{"round_id": r, "url": clip_url(request, r)} for r in store.clip_rounds()]}
        files = sorted(settings.clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {"clips": [{"round_id": f.stem, "url": clip_url(request, f.stem), "bytes": f.stat().st_size}
                          for f in files]}

    if clips.cloud:
        @app.get("/clips/{name}")
        async def cloud_clip(name: str):
            """ส่งต่อไปยังลิงก์ชั่วคราวของ cloud (ขอใหม่เมื่อใกล้หมดอายุ) วิดีโอจึงเล่นจาก cloud โดยตรง"""
            round_id = name.removesuffix(".mp4")
            row = store.round(round_id) if name.endswith(".mp4") and ROUND_ID.fullmatch(round_id) else None
            if not row or not row["clip_file"]:
                raise HTTPException(404, "no clip for this round")
            try:
                link = await run_in_threadpool(clips.signed_url, round_id)
            except UploadFailed as error:
                log.warning("cannot get a link for %s: %s", round_id, error)
                raise HTTPException(503, "the cloud is not reachable right now")
            return RedirectResponse(link, status_code=307)

    # ---------- ผู้เล่น ประวัติ และอันดับ ----------
    # ทุก endpoint ที่ใช้ฐานข้อมูลเป็น async เพื่อให้ SQLite ถูกใช้จาก thread เดียว

    def with_video(request, row):
        name = row.pop("clip_file", None)
        available = bool(name) and (clips.cloud or clips.exists(row["round_id"]))
        return {**row, "video_url": clip_url(request, row["round_id"]) if available else None}

    @app.get("/api/config")
    async def web_config():
        return {"stream_url": settings.stream_url, "grafana_url": settings.grafana_url,
                "track_cm": settings.track_cm}

    def session():
        stats = store.player_stats(hub.player_name, hub.mode) if hub.player_name else None
        return {"player": stats, "next_round_number": store.next_number(), "mode": hub.mode}

    @app.post("/api/mode")
    async def set_mode(body: ModeChoice):
        hub.choose_mode(body.mode)
        return session()

    @app.get("/api/session")
    async def current_session():
        return session()

    @app.post("/api/session")
    async def set_player(body: PlayerName):
        if clean_name(body.name) is None:
            raise HTTPException(422, "name: 1-10 characters, A-Z 0-9 _ - only")
        hub.choose_player(store.select_player(body.name))
        return session()

    @app.delete("/api/session")
    async def clear_player():
        hub.choose_player(None)
        return session()

    @app.get("/api/players")
    async def recent_players(limit: int = Query(4, ge=1, le=50)):
        return {"players": store.recent_players(limit)}

    @app.get("/api/players/{name}")
    async def player_detail(name: str, mode: GameMode = DEFAULT_MODE):
        stats = store.player_stats(name, mode)
        if stats is None:
            raise HTTPException(404, "unknown player")
        return stats

    @app.get("/api/leaderboard")
    async def leaderboard(limit: int = Query(7, ge=1, le=50), mode: GameMode = DEFAULT_MODE):
        return {"entries": store.leaderboard(limit, mode)}

    @app.get("/api/rounds")
    async def round_history(request: Request, result: Literal["all", "win", "loss"] = "all",
                            page: int = Query(1, ge=1, le=100000), page_size: int = Query(7, ge=1, le=50),
                            mode: GameMode | None = None):
        history = store.history(result, page, page_size, mode)
        history["rounds"] = [with_video(request, row) for row in history["rounds"]]
        return history

    @app.get("/api/rounds/{round_id}")
    async def round_detail(request: Request, round_id: str):
        row = store.round(round_id)
        if row is None:
            raise HTTPException(404, "unknown round")
        return {**with_video(request, row), "standing": store.standing(round_id)}

    @app.post("/dev/publish")
    async def dev_publish(request: Request, message: DevMessage):
        """ป้อนข้อความโดยไม่ผ่าน MQTT สำหรับทดสอบบนเครื่องนี้ (เปิดด้วย RLGL_DEV=1)"""
        if not settings.dev_mode:
            raise HTTPException(404)
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "only from this machine")
        if not message.topic.startswith("rlgl/"):
            raise HTTPException(422, "topic must start with rlgl/")
        hub.dispatch(message.topic, message.payload)
        return {"ok": True}

    @app.websocket("/ws")
    async def live(websocket: WebSocket, role: str = "viewer"):
        """role=display คือจอที่ผู้เล่นดู ต้องส่งข้อความใด ๆ มาอย่างน้อยทุก display_timeout วินาที"""
        await websocket.accept()
        queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        queue.put_nowait({"type": "snapshot", "messages": hub.tracker.snapshot()})
        hub.queues.add(queue)
        is_display = role == "display"
        if is_display:
            hub.display_changed(+1)

        async def send():
            while True:
                await websocket.send_json(await queue.get())

        async def receive():
            while True:
                await asyncio.wait_for(websocket.receive_text(),
                                       settings.display_timeout if is_display else None)

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = task.exception()
                if error and not isinstance(error, (WebSocketDisconnect, TimeoutError, RuntimeError, OSError)):
                    log.error("websocket error: %r", error)
        finally:
            # อัปเดตสถานะก่อน await ใด ๆ: ถ้าการเชื่อมต่อถูกยกเลิกกลางทาง จอต้องไม่ค้างเป็น online
            hub.queues.discard(queue)
            if is_display:
                hub.display_changed(-1)
            for task in tasks:
                task.cancel()
            # ใช้ wait ไม่ใช้ gather: gather เปลี่ยนสัญญาณยกเลิกของ server เป็นตัวใหม่
            await asyncio.wait(tasks)
            for task in tasks:
                if not task.cancelled():
                    task.exception()
            with contextlib.suppress(Exception):
                await websocket.close()

    if not clips.cloud:
        app.mount("/clips", StaticFiles(directory=clips.folder), name="clips")
    if settings.web_dir and (settings.web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


def make_app():
    return create_app(Settings.from_env())
