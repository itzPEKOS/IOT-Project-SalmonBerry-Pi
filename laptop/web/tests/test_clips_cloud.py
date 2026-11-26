"""วิดีโอทั้งรอบ: Pi ส่ง JPEG ต่อกัน (MJPEG) -> laptop แปลงเป็น MP4 -> เก็บใน Supabase Storage -> ลบไฟล์ในเครื่อง"""
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import Settings, create_app
from backend.clips import SupabaseClips, UploadFailed
from test_api import MP4, Recorder, inject

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ต้องมี ffmpeg")


class FakeCloud:
    """แทน Supabase ในเทสต์ของ backend: เก็บไฟล์ที่ถูกอัปไว้ในหน่วยความจำ"""
    cloud = True

    def __init__(self, fail=False):
        self.saved, self.fail, self.signed = {}, fail, []

    def save(self, path, round_id):
        if self.fail:
            raise UploadFailed("cloud is down")
        self.saved[round_id] = path.read_bytes()

    def signed_url(self, round_id):
        self.signed.append(round_id)
        return f"https://cloud.example/sign/{round_id}.mp4?token=abc"


def make(tmp_path, clips=None, **overrides):
    options = {"clips_dir": tmp_path / "clips", "temp_dir": tmp_path / "tmp", "dev_mode": True, "mqtt_host": None}
    options.update(overrides)
    recorder = Recorder()
    app = create_app(Settings(**options), publisher=recorder, clips=clips)
    return TestClient(app, client=("127.0.0.1", 50000)), recorder


def send(client, data, round_id="r1", fps=None, name="clip.mjpeg"):
    form = {"round_id": round_id}
    if fps is not None:
        form["fps"] = str(fps)
    return client.post("/clips", data=form, files={"video": (name, data, "application/octet-stream")})


@pytest.fixture(scope="module")
def mjpeg(tmp_path_factory):
    """JPEG 10 เฟรมต่อกัน แบบที่ Pi จะส่ง"""
    if FFMPEG is None:
        pytest.skip("ต้องมี ffmpeg")
    out = tmp_path_factory.mktemp("src") / "frames.mjpeg"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=5", "-t", "2", "-f", "mjpeg", str(out)], check=True, timeout=60)
    return out.read_bytes()


def probe(path):
    result = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name,pix_fmt:format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    stream = info["streams"][0]
    return stream["codec_name"], stream["pix_fmt"], float(info["format"]["duration"])


# ---------- แปลงไฟล์จาก Pi ----------

@needs_ffmpeg
def test_jpeg_frames_become_a_playable_mp4(tmp_path, mjpeg):
    client, _ = make(tmp_path)
    with client:
        response = send(client, mjpeg, fps=5)
        assert response.status_code == 200, response.text
        codec, pix_fmt, duration = probe(tmp_path / "clips" / "r1.mp4")
        assert (codec, pix_fmt) == ("h264", "yuv420p")
        assert duration == pytest.approx(2.0, abs=0.3)  # 10 เฟรม ที่ 5 FPS


@needs_ffmpeg
def test_fps_sets_the_clip_length(tmp_path, mjpeg):
    client, _ = make(tmp_path)
    with client:
        assert send(client, mjpeg, fps=10).status_code == 200
        assert probe(tmp_path / "clips" / "r1.mp4")[2] == pytest.approx(1.0, abs=0.3)


@pytest.mark.parametrize("fps", ["0", "61", "abc", "nan"])
def test_a_bad_fps_is_refused(tmp_path, fps):
    client, recorder = make(tmp_path)
    with client:
        response = client.post("/clips", data={"round_id": "r1", "fps": fps},
                               files={"video": ("clip.mjpeg", b"\xff\xd8\xff\xe0data", "application/octet-stream")})
        assert response.status_code == 422
        assert recorder.of("rlgl/game/media") == []


@needs_ffmpeg
def test_broken_jpeg_data_is_refused_and_nothing_is_stored(tmp_path):
    cloud = FakeCloud()
    client, recorder = make(tmp_path, clips=cloud)
    with client:
        response = send(client, b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 50, fps=15)
        assert response.status_code == 422
        assert cloud.saved == {} and recorder.of("rlgl/game/media") == []
        assert not list((tmp_path / "tmp").rglob("*"))


def test_an_mp4_from_an_older_uploader_is_still_accepted(tmp_path):
    cloud = FakeCloud()
    client, _ = make(tmp_path, clips=cloud)
    with client:
        assert send(client, MP4, name="clip.mp4").status_code == 200
        assert cloud.saved["r1"] == MP4


# ---------- เก็บใน cloud ----------

@needs_ffmpeg
def test_the_clip_goes_to_the_cloud_and_no_copy_stays_on_the_laptop(tmp_path, mjpeg):
    cloud = FakeCloud()
    client, recorder = make(tmp_path, clips=cloud)
    with client:
        response = send(client, mjpeg, fps=5)
        assert response.status_code == 200
        assert cloud.saved["r1"][4:8] == b"ftyp"
        assert not (tmp_path / "clips").exists() or not list((tmp_path / "clips").rglob("*"))
        assert not list((tmp_path / "tmp").rglob("*"))
        (_, payload, _, _), = recorder.of("rlgl/game/media")
        # ลิงก์ใน MQTT เป็นที่อยู่ของ laptop ที่ไม่หมดอายุ ไม่มี token ของ cloud
        assert payload["video_url"] == "http://testserver/clips/r1.mp4"
        assert response.json()["url"] == "http://testserver/clips/r1.mp4"


def test_when_the_cloud_fails_the_pi_is_told_to_keep_the_file_and_retry(tmp_path):
    cloud = FakeCloud(fail=True)
    client, recorder = make(tmp_path, clips=cloud)
    with client:
        response = send(client, MP4, name="clip.mp4")
        assert response.status_code == 503
        assert recorder.of("rlgl/game/media") == []
        assert not list((tmp_path / "tmp").rglob("*"))
        cloud.fail = False
        assert send(client, MP4, name="clip.mp4").status_code == 200
        assert len(recorder.of("rlgl/game/media")) == 1


def test_the_clip_link_redirects_to_a_fresh_cloud_link(tmp_path):
    cloud = FakeCloud()
    client, _ = make(tmp_path, clips=cloud)
    with client:
        inject(client, "rlgl/game/state", {"round_id": "r1", "state": "GREEN_LIGHT"})
        send(client, MP4, name="clip.mp4")
        response = client.get("/clips/r1.mp4", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "https://cloud.example/sign/r1.mp4?token=abc"
        assert client.get("/clips/r9.mp4", follow_redirects=False).status_code == 404
        assert client.get("/clips/..%2Fsecret.mp4", follow_redirects=False).status_code in (404, 422)


def test_round_history_links_to_cloud_clips(tmp_path):
    cloud = FakeCloud()
    client, _ = make(tmp_path, clips=cloud)
    with client:
        inject(client, "rlgl/game/state", {"round_id": "r1", "state": "GREEN_LIGHT"})
        inject(client, "rlgl/game/result", {"round_id": "r1", "result": "WIN", "reason": "touch", "duration_s": 9.0})
        send(client, MP4, name="clip.mp4")
        assert client.get("/api/rounds/r1").json()["video_url"] == "http://testserver/clips/r1.mp4"
        assert client.get("/api/rounds").json()["rounds"][0]["video_url"] == "http://testserver/clips/r1.mp4"
        assert [c["round_id"] for c in client.get("/api/clips").json()["clips"]] == ["r1"]
        assert cloud.signed == []  # ขอลิงก์จาก cloud เฉพาะตอนมีคนเปิดดูจริง


def test_isolated_mode_turns_off_the_broker_and_the_cloud(monkeypatch):
    # ใช้กับ run.cmd -Sim: ทดสอบในเครื่องนี้อย่างเดียว ไม่ว่า .env จะตั้งอะไรไว้
    monkeypatch.setenv("RLGL_MQTT_HOST", "192.168.1.50")
    monkeypatch.setenv("SUPABASE_URL", "https://abcd.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_test")
    monkeypatch.setenv("RLGL_ISOLATED", "1")
    settings = Settings.from_env()
    assert settings.mqtt_host is None
    assert settings.cloud_clips is False
    assert settings.isolated is True


def test_isolated_mode_is_off_unless_asked(monkeypatch):
    monkeypatch.setenv("RLGL_MQTT_HOST", "192.168.1.50")
    monkeypatch.delenv("RLGL_ISOLATED", raising=False)
    settings = Settings.from_env()
    assert (settings.mqtt_host, settings.isolated) == ("192.168.1.50", False)


def test_health_says_when_the_backend_is_isolated(tmp_path):
    client, _ = make(tmp_path, isolated=True)
    with client:
        assert client.get("/api/health").json()["isolated"] is True


def test_settings_turn_on_supabase_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://abcd.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_test")
    monkeypatch.setenv("SUPABASE_BUCKET", "videos")
    settings = Settings.from_env()
    assert (settings.supabase_url, settings.supabase_key, settings.supabase_bucket) == \
           ("https://abcd.supabase.co", "sb_secret_test", "videos")


# ---------- คุยกับ Supabase Storage (จำลองเครือข่าย) ----------

def fake_supabase(handler):
    return httpx.MockTransport(handler)


def test_supabase_upload_sends_the_file_with_the_key(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"Key": "clips/r1.mp4"})

    clip = tmp_path / "r1.mp4"
    clip.write_bytes(MP4)
    store = SupabaseClips("https://abcd.supabase.co/", "eyJhbGciOi.legacy.jwt", "clips", transport=fake_supabase(handler))
    store.save(clip, "r1")
    request, = seen
    assert (request.method, str(request.url)) == ("POST", "https://abcd.supabase.co/storage/v1/object/clips/r1.mp4")
    assert request.headers["apikey"] == "eyJhbGciOi.legacy.jwt"
    assert request.headers["authorization"] == "Bearer eyJhbGciOi.legacy.jwt"
    assert (request.headers["x-upsert"], request.headers["content-type"]) == ("true", "video/mp4")
    assert request.content == MP4


def test_a_new_style_secret_key_is_sent_only_as_apikey(tmp_path):
    seen = []
    clip = tmp_path / "r1.mp4"
    clip.write_bytes(MP4)
    store = SupabaseClips("https://abcd.supabase.co", "sb_secret_xyz", "clips",
                          transport=fake_supabase(lambda r: seen.append(r) or httpx.Response(200, json={})))
    store.save(clip, "r1")
    assert seen[0].headers["apikey"] == "sb_secret_xyz"
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("response", [httpx.Response(400, json={"error": "Bucket not found"}),
                                      httpx.Response(401, json={"error": "Invalid key"})])
def test_supabase_errors_become_upload_failed(tmp_path, response):
    clip = tmp_path / "r1.mp4"
    clip.write_bytes(MP4)
    store = SupabaseClips("https://abcd.supabase.co", "k", "clips", transport=fake_supabase(lambda r: response))
    with pytest.raises(UploadFailed):
        store.save(clip, "r1")


def test_no_internet_becomes_upload_failed(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    clip = tmp_path / "r1.mp4"
    clip.write_bytes(MP4)
    store = SupabaseClips("https://abcd.supabase.co", "k", "clips", transport=fake_supabase(handler))
    with pytest.raises(UploadFailed):
        store.save(clip, "r1")


def test_signed_links_are_made_once_and_reused_until_near_expiry():
    seen, now = [], [1000.0]

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"signedURL": f"/object/sign/clips/r1.mp4?token=t{len(seen)}"})

    store = SupabaseClips("https://abcd.supabase.co", "k", "clips", ttl_s=3600,
                          transport=fake_supabase(handler), clock=lambda: now[0])
    first = store.signed_url("r1")
    assert first == "https://abcd.supabase.co/storage/v1/object/sign/clips/r1.mp4?token=t1"
    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "https://abcd.supabase.co/storage/v1/object/sign/clips/r1.mp4")
    assert json.loads(request.content) == {"expiresIn": 3600}
    now[0] += 1800
    assert store.signed_url("r1") == first and len(seen) == 1
    now[0] += 1700  # เหลือไม่ถึง 5 นาที: ขอใหม่
    assert store.signed_url("r1").endswith("token=t2")


def test_a_failed_signing_is_reported(tmp_path):
    store = SupabaseClips("https://abcd.supabase.co", "k", "clips",
                          transport=fake_supabase(lambda r: httpx.Response(404, json={"error": "not found"})))
    with pytest.raises(UploadFailed):
        store.signed_url("r1")


def test_cloud_links_that_cannot_be_signed_give_503(tmp_path):
    class Broken(FakeCloud):
        def signed_url(self, round_id):
            raise UploadFailed("offline")

    client, _ = make(tmp_path, clips=Broken())
    with client:
        inject(client, "rlgl/game/state", {"round_id": "r1", "state": "GREEN_LIGHT"})
        send(client, MP4, name="clip.mp4")
        assert client.get("/clips/r1.mp4", follow_redirects=False).status_code == 503
