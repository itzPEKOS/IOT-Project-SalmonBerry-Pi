import time

import pytest
from fastapi.testclient import TestClient

from backend.app import Settings, create_app

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


class Recorder:
    """แทน MQTT: เก็บข้อความที่ backend publish"""

    def __init__(self):
        self.messages = []

    def __call__(self, topic, payload, qos, retain):
        self.messages.append((topic, payload, qos, retain))

    def of(self, topic):
        return [m for m in self.messages if m[0] == topic]


def make(tmp_path, client_ip="127.0.0.1", **overrides):
    options = {"clips_dir": tmp_path / "clips", "dev_mode": True, "mqtt_host": None}
    options.update(overrides)
    recorder = Recorder()
    app = create_app(Settings(**options), publisher=recorder)
    return TestClient(app, client=(client_ip, 50000)), recorder


def upload(client, round_id="r1", data=MP4, headers=None):
    return client.post("/clips", data={"round_id": round_id},
                       files={"video": ("clip.mp4", data, "video/mp4")}, headers=headers or {})


def inject(client, topic, payload):
    return client.post("/dev/publish", json={"topic": topic, "payload": payload})


def screen_states(recorder):
    return [m[1]["status"] for m in recorder.of("rlgl/status/web-01")]


def wait_for(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


# ---------- วิดีโอ ----------

def test_upload_saves_the_clip_and_serves_it(tmp_path):
    client, _ = make(tmp_path)
    with client:
        response = upload(client)
        assert response.status_code == 200
        assert response.json()["url"] == "http://testserver/clips/r1.mp4"
        assert (tmp_path / "clips" / "r1.mp4").read_bytes() == MP4
        assert client.get("/clips/r1.mp4").content == MP4
        assert not list((tmp_path / "clips").glob("*.part"))


def test_clip_supports_range_requests_so_the_player_can_seek(tmp_path):
    client, _ = make(tmp_path)
    with client:
        upload(client)
        response = client.get("/clips/r1.mp4", headers={"Range": "bytes=0-9"})
        assert response.status_code == 206
        assert response.content == MP4[:10]


def test_upload_publishes_the_link_not_the_file(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        upload(client)
        (topic, payload, qos, retain), = recorder.of("rlgl/game/media")
        assert (qos, retain) == (1, False)
        assert payload["round_id"] == "r1" and payload["status"] == "ready"
        assert payload["video_url"] == "http://testserver/clips/r1.mp4"
        assert payload["device_id"] == "backend-01"
        assert isinstance(payload["seq"], int) and isinstance(payload["ts"], int)


def test_public_base_url_overrides_the_request_address(tmp_path):
    client, _ = make(tmp_path, public_base_url="http://192.168.1.20:8000/")
    with client:
        assert upload(client).json()["url"] == "http://192.168.1.20:8000/clips/r1.mp4"


@pytest.mark.parametrize("round_id", ["../evil", "a/b", "a\\b", "", "x" * 65, "r1.mp4"])
def test_upload_rejects_unsafe_round_id(tmp_path, round_id):
    client, recorder = make(tmp_path)
    with client:
        assert upload(client, round_id=round_id).status_code in (400, 422)
        assert not list((tmp_path / "clips").iterdir())
        assert recorder.of("rlgl/game/media") == []


def test_upload_rejects_a_file_that_is_not_mp4(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert upload(client, data=b"<html>nope</html>").status_code == 415
        assert not list((tmp_path / "clips").iterdir())


def test_upload_rejects_a_file_over_the_limit(tmp_path):
    client, _ = make(tmp_path, max_upload_bytes=32)
    with client:
        assert upload(client).status_code == 413
        assert not list((tmp_path / "clips").iterdir())


def test_a_failed_retry_keeps_the_earlier_good_clip(tmp_path):
    client, _ = make(tmp_path)
    with client:
        upload(client)
        assert upload(client, data=b"broken data here").status_code == 415
        assert (tmp_path / "clips" / "r1.mp4").read_bytes() == MP4


def test_upload_token_is_required_when_configured(tmp_path):
    client, _ = make(tmp_path, upload_token="test-token")
    with client:
        assert upload(client).status_code == 401
        assert upload(client, headers={"X-Upload-Token": "wrong"}).status_code == 401
        assert upload(client, headers={"X-Upload-Token": "test-token"}).status_code == 200


def test_clip_list(tmp_path):
    client, _ = make(tmp_path)
    with client:
        upload(client, "r1")
        upload(client, "r2")
        listed = client.get("/api/clips").json()["clips"]
        assert sorted(c["round_id"] for c in listed) == ["r1", "r2"]
        assert all(c["url"].endswith(f"/clips/{c['round_id']}.mp4") for c in listed)


# ---------- ข้อมูลสดให้หน้าเว็บ ----------

def test_browser_gets_snapshot_then_live_messages(tmp_path):
    client, _ = make(tmp_path)
    with client:
        inject(client, "rlgl/status/wrist-01", {"status": "online"})
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "snapshot"
            assert first["messages"]["rlgl/status/wrist-01"]["status"] == "online"
            inject(client, "rlgl/gateway/distance", {"distance_cm": 180})
            assert ws.receive_json() == {"type": "message", "topic": "rlgl/gateway/distance",
                                         "payload": {"distance_cm": 180}}


def test_finished_round_sends_summary_and_video_to_browser_and_mqtt(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            inject(client, "rlgl/game/state", {"round_id": "r1", "state": "RED_LIGHT"})
            inject(client, "rlgl/gateway/vision", {"motion_score": 0.2})
            inject(client, "rlgl/game/result",
                   {"round_id": "r1", "result": "GAME_OVER", "reason": "camera"})
            upload(client)
            received = [ws.receive_json() for _ in range(5)]
        assert [m["topic"] for m in received] == [
            "rlgl/game/state", "rlgl/gateway/vision", "rlgl/game/result",
            "rlgl/game/summary", "rlgl/game/media"]
        (_, summary, qos, retain), = recorder.of("rlgl/game/summary")
        assert (summary["round_id"], summary["red_motion_max"], qos, retain) == ("r1", 0.2, 1, False)
        assert summary["device_id"] == "backend-01"


def test_invalid_heart_rate_never_reaches_browser(tmp_path):
    client, _ = make(tmp_path)
    with client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            inject(client, "rlgl/wrist/heartrate", {"bpm": 255})
            inject(client, "rlgl/wrist/heartrate", {"bpm": 90})
            assert ws.receive_json()["payload"] == {"bpm": 90}


def test_two_browsers_receive_the_same_message(tmp_path):
    client, _ = make(tmp_path)
    with client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            a.receive_json(), b.receive_json()
            inject(client, "rlgl/gateway/distance", {"distance_cm": 99})
            assert a.receive_json() == b.receive_json()


def test_health_and_state_endpoints(tmp_path):
    client, _ = make(tmp_path)
    with client:
        inject(client, "rlgl/game/state", {"round_id": "r1", "state": "GREEN_LIGHT"})
        assert client.get("/api/state").json()["messages"]["rlgl/game/state"]["state"] == "GREEN_LIGHT"
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["mqtt_connected"] is False
        assert health["displays"] == 0


# ---------- จอผู้เล่น (ไม่มี LED แล้ว จอเว็บคือสัญญาณเดียว) ----------

def test_screen_is_reported_offline_when_backend_starts(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        (_, payload, qos, retain) = recorder.of("rlgl/status/web-01")[0]
        assert (payload["status"], payload["device_id"], qos, retain) == ("offline", "web-01", 1, True)


def test_only_a_display_connection_makes_the_screen_online(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        with client.websocket_connect("/ws") as viewer:
            viewer.receive_json()
            assert screen_states(recorder) == ["offline"]
        with client.websocket_connect("/ws?role=display") as screen:
            screen.receive_json()
            assert wait_for(lambda: screen_states(recorder)[-1] == "online")
            assert client.get("/api/health").json()["displays"] == 1
        assert wait_for(lambda: screen_states(recorder)[-1] == "offline")
        assert screen_states(recorder) == ["offline", "online", "offline"]


def test_screen_stays_online_while_one_of_two_displays_remains(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        with client.websocket_connect("/ws?role=display") as first:
            first.receive_json()
            with client.websocket_connect("/ws?role=display") as second:
                second.receive_json()
            time.sleep(0.1)
            assert screen_states(recorder) == ["offline", "online"]


def test_silent_display_is_treated_as_offline(tmp_path):
    client, recorder = make(tmp_path, display_timeout=0.3)
    with client:
        with client.websocket_connect("/ws?role=display") as screen:
            screen.receive_json()
            assert wait_for(lambda: screen_states(recorder) == ["offline", "online", "offline"])


def test_display_that_keeps_sending_heartbeats_stays_online(tmp_path):
    client, recorder = make(tmp_path, display_timeout=0.4)
    with client:
        with client.websocket_connect("/ws?role=display") as screen:
            screen.receive_json()
            for _ in range(8):
                screen.send_text("ping")
                time.sleep(0.1)
            assert screen_states(recorder) == ["offline", "online"]


def test_screen_is_reported_offline_on_shutdown(tmp_path):
    client, recorder = make(tmp_path)
    with client:
        pass
    assert screen_states(recorder)[-1] == "offline"
    assert recorder.of("rlgl/status/backend-01")[-1][1]["status"] == "offline"


# ---------- ช่องทดสอบและความปลอดภัย ----------

def test_dev_publish_is_off_by_default(tmp_path):
    client, _ = make(tmp_path, dev_mode=False)
    with client:
        assert inject(client, "rlgl/game/state", {}).status_code == 404


def test_dev_publish_is_refused_from_another_machine(tmp_path):
    client, _ = make(tmp_path, client_ip="192.168.1.50")
    with client:
        assert inject(client, "rlgl/game/state", {}).status_code == 403


def test_a_dev_server_origin_is_allowed_only_when_configured(tmp_path):
    origin = "http://localhost:5173"
    client, _ = make(tmp_path)
    with client:
        assert "access-control-allow-origin" not in client.get("/api/state", headers={"Origin": origin}).headers
    client, _ = make(tmp_path, cors_origins=(origin,))
    with client:
        assert client.get("/api/state", headers={"Origin": origin}).headers["access-control-allow-origin"] == origin
