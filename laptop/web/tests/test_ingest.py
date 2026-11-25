"""ข้อความดิบจาก MQTT ต้องไม่ทำให้ backend ล้ม และข้อความของ backend เองต้องไม่วนกลับมาซ้ำ"""
import pytest

from backend.app import decode_mqtt


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]", b"3", b'"text"', b"", b"\xff\xfe"])
def test_non_object_payloads_are_ignored(raw):
    assert decode_mqtt("rlgl/wrist/imu", raw) is None


@pytest.mark.parametrize("raw", [b'{"acc_peak": NaN}', b'{"acc_peak": Infinity}'])
def test_non_finite_numbers_are_ignored(raw):
    assert decode_mqtt("rlgl/wrist/imu", raw) is None


@pytest.mark.parametrize("topic", ["rlgl/game/media", "rlgl/game/summary", "rlgl/status/web-01",
                                   "rlgl/status/backend-01", "other/topic"])
def test_own_and_foreign_topics_are_ignored(topic):
    assert decode_mqtt(topic, b'{"status": "online"}') is None


def test_normal_message_is_decoded():
    assert decode_mqtt("rlgl/status/wrist-01", b'{"status": "online"}') == {"status": "online"}
