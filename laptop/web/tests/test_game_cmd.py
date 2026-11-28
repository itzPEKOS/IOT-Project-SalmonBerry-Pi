"""หน้าเว็บคุมเกมผ่าน backend: POST /api/cmd → MQTT rlgl/cmd/game → game engine"""
import pytest

from backend.app import Settings, create_app, decode_mqtt
from fastapi.testclient import TestClient
from test_api import make

CMD = "rlgl/cmd/game"


@pytest.mark.parametrize("cmd", ["start", "abort", "green", "red", "touch"])
def test_a_game_command_is_sent_to_the_engine(tmp_path, cmd):
    client, recorder = make(tmp_path)
    with client:
        response = client.post("/api/cmd", json={"cmd": cmd})
        assert response.status_code == 200
        assert response.json() == {"sent": cmd}
        topic, payload, qos, retain = recorder.of(CMD)[-1]
        assert payload["cmd"] == cmd
        assert payload["device_id"] == "backend-01"
        assert (qos, retain) == (1, False)


@pytest.mark.parametrize("body", [{"cmd": "explode"}, {"cmd": ""}, {}, {"cmd": 1}])
def test_unknown_commands_are_refused(tmp_path, body):
    client, recorder = make(tmp_path)
    with client:
        assert client.post("/api/cmd", json=body).status_code == 422
        assert recorder.of(CMD) == []


def test_without_a_broker_the_command_cannot_reach_the_engine(tmp_path):
    settings = Settings(clips_dir=tmp_path / "clips", db_path=tmp_path / "db.sqlite", mqtt_host=None)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/cmd", json={"cmd": "start"})
        assert response.status_code == 503


def test_the_backend_ignores_its_own_command_coming_back_from_the_broker():
    assert decode_mqtt(CMD, b'{"cmd": "start"}') is None
