"""วิดีโอของแต่ละรอบ: แปลง JPEG ต่อกัน (MJPEG จาก Pi) เป็น MP4 แล้วเก็บใน Supabase Storage

ถ้ายังไม่ได้ตั้งค่า Supabase จะเก็บในโฟลเดอร์ clips/ บน laptop แทน (ใช้ตอนพัฒนาและเทสต์)
ฟังก์ชันในไฟล์นี้ทำงานแบบ blocking: backend เรียกผ่าน run_in_threadpool
"""
import os
import pathlib
import shutil
import subprocess
import time
import uuid

import httpx

MP4_TYPE = "video/mp4"
JPEG_START = b"\xff\xd8\xff"
REFRESH_BEFORE_S = 300  # ขอลิงก์ใหม่เมื่อเหลืออายุไม่ถึง 5 นาที


class UploadFailed(Exception):
    """เก็บหรือขอลิงก์จาก cloud ไม่สำเร็จ (ลองใหม่ได้)"""


class ConversionFailed(Exception):
    """ไฟล์ที่ได้รับแปลงเป็นวิดีโอไม่ได้ (ส่งซ้ำก็ไม่หาย)"""


def detect_kind(head):
    """ดูจากไบต์แรก ๆ ว่าเป็น MP4 หรือ JPEG ต่อกัน คืน None ถ้าไม่ใช่ทั้งสองแบบ"""
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(JPEG_START):
        return "mjpeg"
    return None


def mjpeg_to_mp4(src, dst, fps, ffmpeg="ffmpeg", timeout_s=300):
    """JPEG ต่อกัน -> MP4 แบบ H.264, yuv420p ที่ browser เล่นได้ (fps = เฟรมต่อวินาทีที่ Pi อัดได้จริง)"""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mjpeg", "-framerate", f"{fps:g}",
               # JPEG เป็นสีช่วงเต็ม (yuvj420p) ต้องแปลงเป็นช่วงปกติ yuv420p ไม่งั้นบาง browser เล่นไม่ได้
               "-i", str(src), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-color_range", "tv",
               "-movflags", "+faststart", str(dst)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as error:
        raise RuntimeError("ffmpeg is not installed on the laptop") from error
    except subprocess.TimeoutExpired as error:
        raise ConversionFailed("ffmpeg took too long") from error
    if result.returncode != 0 or not pathlib.Path(dst).is_file() or pathlib.Path(dst).stat().st_size == 0:
        raise ConversionFailed((result.stderr or "ffmpeg failed").strip()[-300:])


class LocalClips:
    """เก็บใน laptop (ใช้เมื่อไม่ได้ตั้ง Supabase)"""
    cloud = False

    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def save(self, path, round_id):
        partial = self.folder / f"{round_id}.{uuid.uuid4().hex[:8]}.part"
        try:
            shutil.copyfile(path, partial)
            try:
                partial.replace(self.folder / f"{round_id}.mp4")
            except PermissionError as error:  # Windows: คลิปเก่ากำลังถูกเล่นอยู่
                raise UploadFailed("the old clip is in use, try again") from error
        finally:
            partial.unlink(missing_ok=True)

    def exists(self, round_id):
        return (self.folder / f"{round_id}.mp4").is_file()


class SupabaseClips:
    """เก็บใน Supabase Storage (bucket แบบ private) หน้าเว็บเล่นผ่านลิงก์ชั่วคราว"""
    cloud = True

    def __init__(self, url, key, bucket="clips", ttl_s=3600, transport=None, clock=time.time, timeout_s=120):
        self.base = f"{url.rstrip('/')}/storage/v1"
        self.bucket, self.ttl_s, self.clock = bucket, ttl_s, clock
        headers = {"apikey": key}
        # กุญแจแบบเก่าเป็น JWT ส่งใน Authorization ได้ ส่วนกุญแจแบบใหม่ (sb_secret_...) ส่งเป็น apikey อย่างเดียว
        if not key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.Client(headers=headers, transport=transport, timeout=timeout_s)
        self.links = {}  # round_id -> (ลิงก์, หมดอายุเมื่อ)

    def _call(self, method, path, **kwargs):
        try:
            response = self.http.request(method, f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as error:
            raise UploadFailed(f"cannot reach Supabase: {error}") from error
        if response.status_code >= 300:
            raise UploadFailed(f"Supabase answered {response.status_code}: {response.text[:200]}")
        return response

    def save(self, path, round_id):
        with open(path, "rb") as handle:
            self._call("POST", f"/object/{self.bucket}/{round_id}.mp4", content=handle.read(),
                       headers={"Content-Type": MP4_TYPE, "x-upsert": "true", "Cache-Control": "3600"})
        self.links.pop(round_id, None)

    def signed_url(self, round_id):
        cached = self.links.get(round_id)
        now = self.clock()
        if cached and cached[1] - now > REFRESH_BEFORE_S:
            return cached[0]
        response = self._call("POST", f"/object/sign/{self.bucket}/{round_id}.mp4", json={"expiresIn": self.ttl_s})
        try:
            relative = response.json()["signedURL"]
        except (ValueError, KeyError, TypeError) as error:
            raise UploadFailed("Supabase did not return a signed link") from error
        link = f"{self.base}{relative}"
        self.links[round_id] = (link, now + self.ttl_s)
        return link


def remove_tree(folder):
    """ลบโฟลเดอร์ชั่วคราว (ไม่เก็บสำเนาคลิปไว้ในเครื่อง)"""
    shutil.rmtree(folder, ignore_errors=True)
    if os.path.exists(folder):  # Windows อาจยังถือไฟล์อยู่ชั่วครู่
        time.sleep(0.2)
        shutil.rmtree(folder, ignore_errors=True)
