"""กติกาของ game engine (ไม่ต่อ MQTT จริง: ป้อนข้อความเข้าและดูข้อความที่ส่งออก)"""
import random

import pytest

from engine.core import (AIM, BUTTON, CALIBRATION, CMD, DISTANCE, FINISH, HEARTRATE, IMU, MODE, RESULT, STATE,
                         VISION, Config, Engine)

START_T = 1795686400.0


class Clock:
    def __init__(self):
        self.t = START_T

    def __call__(self):
        return self.t


def make(**overrides):
    # test ทั่วไปไม่บังคับ sensor (sensors=()) test เรื่องเช็ก sensor ระบุเอง
    settings = dict(calib_s=5.0, green_s=(4.0, 4.0), transition_s=1.5, red_s=(3.0, 3.0), limit_s=60.0,
                    intro_s=2.0, count_s=3.0, sensors=())
    settings.update(overrides)
    clock, out = Clock(), []
    engine = Engine(Config(**settings), publish=lambda topic, body, qos=0, retain=False: out.append(
        (topic, body, qos, retain)), clock=clock, rng=random.Random(1))
    return engine, clock, out


def online(engine, *devices, status="online"):
    for device in devices:
        engine.message(f"rlgl/status/{device}", {"device_id": device, "status": status})


def ready(engine):
    online(engine, "backend-01", "web-01", "pi", "wrist-01")


def run(engine, clock, seconds, step=0.05):
    for _ in range(round(seconds / step)):
        clock.t += step
        engine.tick()


def run_with_sensors(engine, clock, seconds, imu=True, bpm=72, player=True, distance=None, step=0.05):
    """เดินเวลาพร้อมป้อนข้อมูล sensor: IMU 20 ครั้ง/วิ, ชีพจรและกล้อง 1 ครั้ง/วิ"""
    for i in range(round(seconds / step)):
        clock.t += step
        if imu:
            engine.message(IMU, {"acc_peak": 0.04, "gyro_rms": 2.0})
        if i % 20 == 0:
            if bpm is not None:
                engine.message(HEARTRATE, {"bpm": bpm})
            engine.message(VISION, {"event": "heartbeat", "player_seen": player, "visible_joints": 17 if player else 0})
            if distance is not None:
                engine.message(DISTANCE, {"distance_cm": distance})
        engine.tick()


def sent(out, topic):
    return [body for t, body, _, _ in out if t == topic]


def states(out):
    names = []
    for body in sent(out, STATE):
        if not names or names[-1] != body["state"]:
            names.append(body["state"])
    return names


def start(engine, clock, out):
    """กดครั้งแรก: เริ่ม calibrate"""
    ready(engine)
    ok, round_id = engine.command("start")
    assert ok, round_id
    return round_id


def to_green(engine, clock, out):
    """calibrate 5 วิ → READY → กดเริ่ม → นับถอยหลัง 5 วิ → ไฟเขียวแรก"""
    round_id = start(engine, clock, out)
    run(engine, clock, 5.05)
    assert engine.state == "READY"
    assert engine.command("start")[0]
    run(engine, clock, 5.0)
    assert engine.state == "GREEN_LIGHT"
    return round_id


def to_red(engine, clock, out):
    """ไฟเขียว 4 + หัน 1.5 แล้วเข้าไฟแดงครั้งแรก"""
    round_id = to_green(engine, clock, out)
    run(engine, clock, 5.55)
    assert engine.state == "RED_LIGHT"
    return round_id


def result(out):
    bodies = sent(out, RESULT)
    assert len(bodies) == 1, bodies
    return bodies[0]


# ---------- เริ่มรอบ ----------

def test_announce_clears_an_old_retained_state():
    engine, _, out = make()
    engine.announce()
    topic, body, qos, retain = out[-1]
    assert (topic, body["state"], qos, retain) == (STATE, "IDLE", 1, True)


def test_the_default_round_is_30_seconds_and_checks_wrist_heart_and_camera():
    config = Config()
    assert config.limit_s == 30.0
    assert config.sensors == ("imu", "heart", "camera")


def test_start_is_refused_until_the_screen_backend_camera_and_wrist_are_online():
    engine, _, out = make()
    online(engine, "backend-01", "web-01")
    ok, reason = engine.command("start")
    assert not ok
    assert "pi" in reason and "wrist-01" in reason
    assert sent(out, STATE) == []


def test_the_pi_may_report_as_gateway_01():
    engine, _, _ = make()
    online(engine, "backend-01", "web-01", "gateway-01", "wrist-01")
    assert engine.command("start")[0]


def test_required_devices_can_be_turned_off_for_testing():
    engine, _, _ = make(require=())
    assert engine.command("start")[0]


def test_start_sends_calibrate_with_a_numeric_round_id_and_the_chosen_mode():
    engine, clock, out = make()
    engine.message(MODE, {"mode": "sniper"})
    round_id = start(engine, clock, out)
    body = sent(out, STATE)[-1]
    assert body["state"] == "CALIBRATE"
    assert body["round_id"] == round_id == int(START_T)
    assert body["mode"] == "sniper"
    assert out[-1][2:] == (1, True)  # QoS 1, retain


def test_round_ids_never_repeat_even_when_rounds_start_in_the_same_second():
    engine, clock, out = make()
    first = start(engine, clock, out)
    engine.command("abort")
    second = engine.command("start")[1]
    assert second > first


def test_the_station_button_and_the_web_command_start_a_round():
    for topic, body in ((BUTTON, {"pressed": 1}), (CMD, {"cmd": "start"})):
        engine, _, out = make()
        ready(engine)
        engine.message(topic, body)
        assert engine.state == "CALIBRATE", topic


def test_start_is_ignored_while_calibrating_or_counting_down():
    engine, clock, out = make()
    first = start(engine, clock, out)
    assert not engine.command("start")[0]
    run(engine, clock, 5.05)
    engine.command("start")
    run(engine, clock, 1.0)
    assert engine.state == "COUNTDOWN"
    assert not engine.command("start")[0]
    assert engine.round_id == first


# ---------- calibrate และเช็ก sensor ----------

def test_calibration_progress_is_sent_twice_a_second_with_the_resting_heart_rate():
    engine, clock, out = make()
    round_id = start(engine, clock, out)
    engine.message(VISION, {"event": "heartbeat", "player_seen": True})
    for bpm in (70, 0, 74):  # 0 = ESP ยังวัดไม่ได้
        engine.message(HEARTRATE, {"bpm": bpm})
    run(engine, clock, 5.0)
    progress = sent(out, CALIBRATION)
    assert 9 <= len(progress) <= 11
    assert progress[0]["round_id"] == round_id
    assert progress[0]["player_detected"] == 1
    assert progress[-1]["progress"] == 1.0
    assert progress[-1]["done"] == 1
    assert progress[-1]["baseline_s"] == 5.0
    engine.command("abort")
    assert result(out)["bpm_rest"] == 72


def test_calibration_passes_when_every_required_sensor_sends_data():
    engine, clock, out = make(sensors=("imu", "heart", "camera"))
    start(engine, clock, out)
    run_with_sensors(engine, clock, 5.05)
    assert engine.state == "READY"
    final = sent(out, CALIBRATION)[-1]
    assert (final["done"], final["ok"], final["failed"]) == (1, 1, "")
    assert (final["check_imu"], final["check_heart"], final["check_camera"]) == ("ok", "ok", "ok")
    # sensor ที่ไม่บังคับ: off ถ้าไม่มีข้อมูล
    assert (final["check_distance"], final["check_station"]) == ("off", "off")


def test_live_checks_show_wait_until_a_sensor_has_sent_enough():
    engine, clock, out = make(sensors=("imu", "heart", "camera"))
    start(engine, clock, out)
    run_with_sensors(engine, clock, 0.5, bpm=None)
    early = sent(out, CALIBRATION)[-1]
    assert early["check_heart"] == "wait"
    assert early["check_camera"] == "ok"
    assert early["done"] == 0


@pytest.mark.parametrize("missing, kwargs", [("imu", {"imu": False}), ("heart", {"bpm": 0}),
                                             ("camera", {"player": False})])
def test_calibration_fails_when_a_required_sensor_is_silent(missing, kwargs):
    engine, clock, out = make(sensors=("imu", "heart", "camera"))
    round_id = start(engine, clock, out)
    run_with_sensors(engine, clock, 5.05, **kwargs)
    assert engine.state == "CALIBRATE_FAILED"
    final = sent(out, CALIBRATION)[-1]
    assert (final["done"], final["ok"], final["failed"]) == (1, 0, missing)
    assert final[f"check_{missing}"] == "fail"
    assert sent(out, STATE)[-1]["round_id"] == round_id
    assert sent(out, RESULT) == []


def test_pressing_again_after_a_failed_check_recalibrates_the_same_round():
    engine, clock, out = make(sensors=("imu", "heart", "camera"))
    round_id = start(engine, clock, out)
    run_with_sensors(engine, clock, 5.05, imu=False)
    assert engine.state == "CALIBRATE_FAILED"
    ok, again = engine.command("start")
    assert ok and again == round_id
    assert engine.state == "CALIBRATE"
    run_with_sensors(engine, clock, 5.05)
    assert engine.state == "READY"


def test_distance_and_station_can_be_required_later():
    engine, clock, out = make(sensors=("imu", "heart", "camera", "distance", "station"))
    start(engine, clock, out)
    run_with_sensors(engine, clock, 5.05, distance=290)
    final = sent(out, CALIBRATION)[-1]
    assert final["check_distance"] == "ok"
    assert final["check_station"] == "fail" and final["failed"] == "station"
    online(engine, "station-01")
    engine.command("start")
    run_with_sensors(engine, clock, 5.05, distance=290)
    assert engine.state == "READY"


# ---------- READY และนับถอยหลัง ----------

def test_ready_waits_for_the_second_press():
    engine, clock, out = make()
    start(engine, clock, out)
    run(engine, clock, 65.0)
    assert engine.state == "READY"
    assert sent(out, STATE)[-1]["remaining_s"] == 60


def test_the_second_press_plays_the_intro_and_counts_down_before_green():
    engine, clock, out = make(intro_s=2.0, count_s=3.0)
    start(engine, clock, out)
    run(engine, clock, 5.05)
    engine.command("start")
    body = sent(out, STATE)[-1]
    assert (body["state"], body["starts_in_s"], body["intro_s"], body["count_s"]) == ("COUNTDOWN", 5.0, 2.0, 3.0)
    run(engine, clock, 4.9)
    assert engine.state == "COUNTDOWN"
    run(engine, clock, 0.15)
    assert engine.state == "GREEN_LIGHT"
    assert sent(out, STATE)[-1]["remaining_s"] == 60  # เวลาเล่นเริ่มนับตอนไฟเขียว


def test_after_calibrate_the_lights_cycle_green_turn_red():
    engine, clock, out = make()
    to_green(engine, clock, out)
    run(engine, clock, 4 + 1.5 + 3 + 0.05)
    assert states(out) == ["CALIBRATE", "READY", "COUNTDOWN", "GREEN_LIGHT", "TRANSITION", "RED_LIGHT",
                           "GREEN_LIGHT"]


def test_the_state_is_repeated_every_second_with_the_time_left():
    engine, clock, out = make()
    to_green(engine, clock, out)
    before = len(sent(out, STATE))
    run(engine, clock, 3.0)
    green = sent(out, STATE)[before - 1:]
    assert all(body["state"] == "GREEN_LIGHT" for body in green)
    assert len(green) >= 3
    assert green[0]["remaining_s"] == 60
    assert green[-1]["remaining_s"] == 57


# ---------- ตัดสินไฟแดง ----------

def test_camera_movement_on_red_is_game_over_with_the_body_part():
    engine, clock, out = make()
    round_id = to_red(engine, clock, out)
    run(engine, clock, 1.0)
    engine.message(VISION, {"event": "moved", "round_id": round_id, "part": "left_arm", "joint": "left_wrist",
                            "score": 1.374})
    engine.tick()
    body = result(out)
    assert body["result"] == "GAME_OVER" and body["reason"] == "camera"
    assert body["caught_part"] == "left_arm"
    assert body["caught_score_ratio"] == 1.37
    assert body["caught_red_s"] == pytest.approx(1.0, abs=0.15)
    # ชื่อ field ที่ pi_client.py อ่านตอนทำคลิป
    assert (body["cause"], body["part"], body["score"]) == ("camera", "left_arm", 1.37)
    assert states(out)[-1] == "GAME_OVER"


def test_the_result_is_sent_before_the_final_state():
    # pi_client.py อ่านผลล่าสุดตอนได้ state GAME_OVER
    engine, clock, out = make()
    round_id = to_red(engine, clock, out)
    engine.message(VISION, {"event": "moved", "round_id": round_id, "part": "head", "score": 1.2})
    engine.tick()
    topics = [t for t, *_ in out]
    assert topics.index(RESULT) < len(topics) - 1
    assert topics[-1] == STATE


def test_camera_movement_outside_red_or_from_an_old_round_is_ignored():
    engine, clock, out = make()
    round_id = to_green(engine, clock, out)
    run(engine, clock, 1.0)
    engine.message(VISION, {"event": "moved", "round_id": round_id, "part": "head", "score": 2.0})
    run(engine, clock, 4.6)
    assert engine.state == "RED_LIGHT"
    engine.message(VISION, {"event": "moved", "round_id": round_id - 1, "part": "head", "score": 2.0})
    engine.tick()
    assert sent(out, RESULT) == []


def test_wrist_movement_on_red_needs_two_samples_in_a_row():
    engine, clock, out = make()
    to_red(engine, clock, out)
    run(engine, clock, 0.5)
    engine.message(IMU, {"acc_peak": 0.6, "gyro_rms": 10})
    engine.message(IMU, {"acc_peak": 0.05, "gyro_rms": 3})
    engine.tick()
    assert sent(out, RESULT) == []
    engine.message(IMU, {"acc_peak": 0.1, "gyro_rms": 80})
    engine.message(IMU, {"acc_peak": 0.5, "gyro_rms": 5})
    engine.tick()
    body = result(out)
    assert (body["result"], body["reason"]) == ("GAME_OVER", "imu")
    assert "caught_part" not in body


def test_wrist_samples_right_after_the_light_turns_red_are_ignored():
    # ค่าที่วัดตอนยังหันกล้องอยู่อาจมาถึงช้า
    engine, clock, out = make(imu_grace_s=0.3)
    to_red(engine, clock, out)
    for _ in range(3):
        engine.message(IMU, {"acc_peak": 0.9, "gyro_rms": 100})
    engine.tick()
    assert sent(out, RESULT) == []


def test_camera_and_wrist_together_is_reason_both():
    engine, clock, out = make()
    round_id = to_red(engine, clock, out)
    run(engine, clock, 0.5)
    engine.message(IMU, {"acc_peak": 0.7, "gyro_rms": 5})
    engine.message(IMU, {"acc_peak": 0.7, "gyro_rms": 5})
    engine.message(VISION, {"event": "moved", "round_id": round_id, "part": "torso", "score": 1.1})
    engine.tick()
    assert result(out)["reason"] == "both"


def test_movement_on_green_and_during_the_countdown_is_allowed():
    engine, clock, out = make()
    start(engine, clock, out)
    run(engine, clock, 5.05)
    engine.command("start")
    for _ in range(5):
        engine.message(IMU, {"acc_peak": 0.9, "gyro_rms": 120})
    run(engine, clock, 6.0)
    assert engine.state == "GREEN_LIGHT"
    for _ in range(5):
        engine.message(IMU, {"acc_peak": 0.9, "gyro_rms": 120})
    engine.tick()
    assert sent(out, RESULT) == []


# ---------- เส้นชัยและเวลา ----------

def test_touching_the_finish_on_green_wins():
    engine, clock, out = make()
    round_id = to_green(engine, clock, out)
    run(engine, clock, 2.0)
    engine.message(FINISH, {"touched": 1, "round_id": round_id})
    engine.tick()
    body = result(out)
    assert (body["result"], body["reason"]) == ("WIN", "touch")
    assert body["duration_s"] == pytest.approx(2.0, abs=0.1)  # นับจากไฟเขียวแรก
    assert body["started_at"] == int(START_T * 1000)
    assert states(out)[-1] == "WIN"


def test_touching_the_finish_before_green_does_nothing():
    engine, clock, out = make()
    start(engine, clock, out)
    run(engine, clock, 5.05)
    engine.command("touch")
    engine.command("start")
    engine.command("touch")
    engine.tick()
    assert sent(out, RESULT) == []


def test_touching_the_finish_on_red_is_game_over():
    engine, clock, out = make()
    to_red(engine, clock, out)
    engine.command("touch")
    engine.tick()
    assert (result(out)["result"], result(out)["reason"]) == ("GAME_OVER", "finish_during_red")


def test_running_out_of_time_is_game_over():
    engine, clock, out = make(limit_s=10.0)
    to_green(engine, clock, out)
    run(engine, clock, 10.1)
    body = result(out)
    assert (body["result"], body["reason"]) == ("GAME_OVER", "timeout")
    assert body["duration_s"] == 10.0


def test_the_result_stays_until_the_next_round_starts():
    # หน้าเว็บแสดงผลรอบจนกว่าจะกด PLAY AGAIN หรือรอบใหม่เริ่ม จึงไม่กลับ IDLE เอง
    engine, clock, out = make()
    first = to_green(engine, clock, out)
    run(engine, clock, 1.0)
    engine.command("touch")
    run(engine, clock, 120.0)
    assert engine.state == "WIN"
    assert sent(out, STATE)[-1]["state"] == "WIN"
    clock.t += 1
    ok, second = engine.command("start")
    assert ok and second > first
    assert sent(out, STATE)[-1]["state"] == "CALIBRATE"


# ---------- ยกเลิกรอบ ----------

@pytest.mark.parametrize("device, reason", [("web-01", "disconnect"), ("backend-01", "disconnect"),
                                            ("wrist-01", "disconnect"), ("pi", "camera_lost")])
def test_a_device_dropping_out_mid_round_aborts_it(device, reason):
    engine, clock, out = make()
    to_green(engine, clock, out)
    online(engine, device, status="offline")
    engine.tick()
    body = result(out)
    assert (body["result"], body["reason"]) == ("ABORTED", reason)
    assert engine.state == "IDLE"
    assert sent(out, STATE)[-1]["state"] == "IDLE"


def test_a_device_dropping_out_while_ready_also_aborts():
    engine, clock, out = make()
    start(engine, clock, out)
    run(engine, clock, 5.05)
    online(engine, "wrist-01", status="offline")
    assert (result(out)["result"], engine.state) == ("ABORTED", "IDLE")


def test_the_operator_can_abort():
    engine, clock, out = make()
    start(engine, clock, out)
    engine.message(CMD, {"cmd": "abort"})
    assert (result(out)["result"], engine.state) == ("ABORTED", "IDLE")


def test_losing_the_broker_aborts_with_reason_disconnect():
    engine, clock, out = make()
    start(engine, clock, out)
    assert engine.abort("disconnect")[0]
    assert result(out)["reason"] == "disconnect"
    assert not engine.abort("disconnect")[0]  # ไม่มีรอบแล้ว


# ---------- โหมด sniper ----------

def sniper_round(**overrides):
    engine, clock, out = make(**overrides)
    engine.message(MODE, {"mode": "sniper"})
    round_id = start(engine, clock, out)
    run(engine, clock, 5.05)
    assert engine.state == "READY"
    engine.command("start")
    run(engine, clock, 5.0)
    assert engine.state == "SNIPER"
    return engine, clock, out, round_id


def lock(engine, round_id, seconds):
    engine.message(AIM, {"round_id": round_id, "in_box": 1, "lock_s": seconds, "lock_limit_s": 1.0})


def test_sniper_starts_with_three_lives_after_the_countdown():
    engine, _, out, _ = sniper_round()
    body = sent(out, STATE)[-1]
    assert (body["state"], body["mode"], body["lives"], body["hits"], body["dodges"]) == ("SNIPER", "sniper", 3, 0, 0)
    assert "COUNTDOWN" in states(out)


def test_a_full_lock_is_one_hit_and_is_sent_at_once():
    engine, clock, out, round_id = sniper_round()
    lock(engine, round_id, 0.5)
    lock(engine, round_id, 1.0)
    lock(engine, round_id, 1.0)  # ยังล็อกอยู่ ไม่นับซ้ำ
    body = sent(out, STATE)[-1]
    assert (body["lives"], body["hits"]) == (2, 1)
    lock(engine, round_id, 0.0)
    lock(engine, round_id, 1.0)
    assert sent(out, STATE)[-1]["hits"] == 2


def test_three_hits_lose_the_sniper_round():
    engine, clock, out, round_id = sniper_round()
    for _ in range(3):
        lock(engine, round_id, 1.0)
        lock(engine, round_id, 0.0)
    engine.tick()
    body = result(out)
    assert (body["result"], body["reason"], body["mode"], body["hits"]) == ("GAME_OVER", "hits", "sniper", 3)


def test_fast_wrist_moves_count_as_dodges_and_never_lose():
    engine, clock, out, round_id = sniper_round(dodge_gap_s=0.5)
    for _ in range(4):
        engine.message(IMU, {"acc_peak": 0.9, "gyro_rms": 150})
        engine.message(IMU, {"acc_peak": 0.1, "gyro_rms": 5})
        run(engine, clock, 0.6)
    engine.message(FINISH, {"touched": 1})
    engine.tick()
    body = result(out)
    assert (body["result"], body["reason"], body["dodges"], body["hits"]) == ("WIN", "touch", 4, 0)


def test_the_sniper_clock_runs_out():
    engine, clock, out, _ = sniper_round(limit_s=20.0)
    run(engine, clock, 20.1)
    assert (result(out)["result"], result(out)["reason"]) == ("GAME_OVER", "timeout")
