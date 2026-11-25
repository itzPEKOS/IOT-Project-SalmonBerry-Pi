"""Backend ของเกม: รับ MQTT -> ส่งให้หน้าเว็บ, สรุปแต่ละรอบ, รับและให้บริการวิดีโอ

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
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.tracker import MEDIA, SUMMARY, GameTracker

log = logging.getLogger("uvicorn.error")

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent
LAPTOP_DIR = WEB_DIR.parent
BACKEND_ID, SCREEN_ID = "backend-01", "web-01"
BACKEND_STATUS = f"rlgl/status/{BACKEND_ID}"
SCREEN_STATUS = f"rlgl/status/{SCREEN_ID}"
OWN_TOPICS = {SUMMARY, MEDIA, BACKEND_STATUS, SCREEN_STATUS}
ROUND_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
CHUNK = 1024 * 1024
QUEUE_LIMIT = 500
KEEPALIVE_S = 5


@dataclass
class Settings:
    clips_dir: pathlib.Path = WEB_DIR / "clips"
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
        return cls(
            clips_dir=pathlib.Path(values.get("RLGL_CLIPS_DIR") or WEB_DIR / "clips"),
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

    def __init__(self, publisher):
        self.publisher = publisher
        self.tracker = GameTracker()
        self.queues = set()
        self.displays = 0
        self.seq = {}

    def dispatch(self, topic, payload):
        outcome = self.tracker.handle(topic, payload)
        if outcome.accepted:
            self._broadcast({"type": "message", "topic": topic, "payload": payload})
        for out_topic, body in outcome.outgoing:
            self.send(out_topic, body)

    def send(self, topic, body, device=BACKEND_ID, qos=1, retain=False):
        """ข้อความที่ backend สร้างเอง: ส่งให้ browser และ publish เข้า MQTT"""
        self.seq[device] = self.seq.get(device, 0) + 1
        payload = {"device_id": device, "seq": self.seq[device], "ts": int(time.time() * 1000), **body}
        self.dispatch(topic, payload)
        self.publisher(topic, payload, qos, retain)

    def send_status(self, topic, device, online):
        self.send(topic, {"status": "online" if online else "offline"}, device=device, retain=True)

    def announce(self):
        self.send_status(BACKEND_STATUS, BACKEND_ID, True)
        self.send_status(SCREEN_STATUS, SCREEN_ID, self.displays > 0)

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


class DevMessage(BaseModel):
    topic: str
    payload: dict


def create_app(settings, publisher=None):
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    link = MqttLink(settings) if publisher is None and settings.mqtt_host else None
    hub = Hub(publisher or (link.publish if link else lambda *args: None))

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

    app = FastAPI(title="Red Light / Green Light backend", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=["GET"], allow_headers=["*"])

    @app.middleware("http")
    async def reject_huge_uploads(request, call_next):
        # ปฏิเสธก่อนรับไฟล์ทั้งก้อน (เผื่อ 64 KB ให้ส่วนหัวของ multipart)
        length = request.headers.get("content-length", "")
        if request.url.path == "/clips" and length.isdigit() and int(length) > settings.max_upload_bytes + 65536:
            return JSONResponse({"detail": "file too large"}, status_code=413)
        return await call_next(request)

    def clip_url(request, round_id):
        base = settings.public_base_url or str(request.base_url)
        return f"{base.rstrip('/')}/clips/{round_id}.mp4"

    @app.post("/clips")
    async def receive_clip(request: Request, round_id: str = Form(...), video: UploadFile = File(...)):
        if settings.upload_token and not hmac.compare_digest(
                request.headers.get("x-upload-token", "").encode(), settings.upload_token.encode()):
            raise HTTPException(401, "missing or wrong X-Upload-Token")
        if not ROUND_ID.fullmatch(round_id):
            raise HTTPException(422, "round_id may only use A-Z a-z 0-9 _ - (max 64)")

        partial = settings.clips_dir / f"{round_id}.{uuid.uuid4().hex[:8]}.part"
        try:
            size = 0
            with partial.open("wb") as out:
                while chunk := await video.read(CHUNK):
                    if size == 0 and chunk[4:8] != b"ftyp":
                        raise HTTPException(415, "not an MP4 file")
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "file too large")
                    # เขียนดิสก์ใน thread แยก เพื่อไม่ให้จอผู้เล่นได้รับ state ช้า
                    await run_in_threadpool(out.write, chunk)
            if size == 0:
                raise HTTPException(415, "empty file")
            try:
                partial.replace(settings.clips_dir / f"{round_id}.mp4")
            except PermissionError:
                raise HTTPException(503, "the old clip is in use, try again")
        finally:
            partial.unlink(missing_ok=True)

        url = clip_url(request, round_id)
        hub.send(MEDIA, {"round_id": round_id, "status": "ready", "video_url": url})
        return {"url": url}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mqtt_connected": bool(link and link.connected), "displays": hub.displays}

    @app.get("/api/state")
    async def state():
        return {"messages": hub.tracker.snapshot()}

    @app.get("/api/clips")
    def clips(request: Request):
        files = sorted(settings.clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {"clips": [{"round_id": f.stem, "url": clip_url(request, f.stem), "bytes": f.stat().st_size}
                          for f in files]}

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

    app.mount("/clips", StaticFiles(directory=settings.clips_dir), name="clips")
    if settings.web_dir and (settings.web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


def make_app():
    return create_app(Settings.from_env())
