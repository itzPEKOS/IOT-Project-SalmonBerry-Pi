import pytest

from backend.tracker import GameTracker


def state(name, round_id="r1"):
    return "rlgl/game/state", {"round_id": round_id, "state": name, "remaining_s": 30}


def result(round_id="r1", outcome="GAME_OVER", reason="camera"):
    return "rlgl/game/result", {"round_id": round_id, "result": outcome, "reason": reason}


def feed(tracker, *messages):
    out = []
    for topic, payload in messages:
        out.extend(tracker.handle(topic, payload).outgoing)
    return out


def bpm(value):
    return "rlgl/wrist/heartrate", {"bpm": value}


def vision(score):
    return "rlgl/gateway/vision", {"motion_score": score}


def imu(peak):
    return "rlgl/wrist/imu", {"acc_peak": peak}


def test_summary_counts_motion_only_during_red_light():
    t = GameTracker()
    out = feed(
        t,
        state("CALIBRATE"), bpm(70),
        state("GREEN_LIGHT"), bpm(100), vision(9.0), imu(3.0),
        state("TRANSITION"), vision(8.0),
        state("RED_LIGHT"), bpm(120), vision(0.2), vision(0.7), imu(0.4),
        state("GAME_OVER"),
        result(),
    )
    assert len(out) == 1
    topic, summary = out[0]
    assert topic == "rlgl/game/summary"
    assert summary["round_id"] == "r1"
    assert summary["bpm_avg"] == 110.0
    assert summary["bpm_max"] == 120
    assert summary["red_motion_max"] == 0.7
    assert summary["red_acc_peak_max"] == 0.4
    assert summary["red_light_count"] == 1


def test_repeated_red_light_state_messages_count_as_one_period():
    t = GameTracker()
    out = feed(t, state("RED_LIGHT"), state("RED_LIGHT"), state("GREEN_LIGHT"),
               state("RED_LIGHT"), result())
    assert out[0][1]["red_light_count"] == 2


def test_values_without_data_are_left_out():
    out = feed(GameTracker(), state("GREEN_LIGHT"), result(outcome="WIN", reason="touch"))
    summary = out[0][1]
    for key in ("bpm_avg", "bpm_max", "red_motion_max", "red_acc_peak_max"):
        assert key not in summary
    assert summary["red_light_count"] == 0


def test_duplicate_result_produces_one_summary():
    out = feed(GameTracker(), state("RED_LIGHT"), result(), result())
    assert len(out) == 1


def test_result_for_a_round_never_seen_produces_no_summary():
    assert feed(GameTracker(), result(round_id="old")) == []


def test_heart_rate_outside_range_is_rejected():
    t = GameTracker()
    feed(t, state("GREEN_LIGHT"))
    for value in (0, 44, 181, 250, "90", None, True):
        assert t.handle(*bpm(value)).accepted is False, value
    assert t.handle(*bpm(45)).accepted is True
    assert t.handle(*bpm(180)).accepted is True
    assert feed(t, result())[0][1]["bpm_max"] == 180


def test_new_round_clears_previous_result_summary_and_video():
    t = GameTracker()
    feed(t, state("RED_LIGHT"), result())
    t.handle("rlgl/game/summary", {"round_id": "r1"})
    t.handle("rlgl/game/media", {"round_id": "r1", "video_url": "x"})
    feed(t, state("CALIBRATE", round_id="r2"))
    latest = t.snapshot()
    for topic in ("rlgl/game/result", "rlgl/game/summary", "rlgl/game/media"):
        assert topic not in latest
    assert latest["rlgl/game/state"]["round_id"] == "r2"


def test_new_round_does_not_inherit_old_numbers():
    t = GameTracker()
    feed(t, state("RED_LIGHT"), bpm(170), vision(5.0), result())
    out = feed(t, state("RED_LIGHT", round_id="r2"), bpm(80), vision(0.1), result(round_id="r2"))
    assert out[0][1]["bpm_max"] == 80
    assert out[0][1]["red_motion_max"] == 0.1


def test_snapshot_keeps_each_device_status():
    t = GameTracker()
    t.handle("rlgl/status/wrist-01", {"status": "online"})
    t.handle("rlgl/status/station-01", {"status": "offline"})
    snap = t.snapshot()
    assert snap["rlgl/status/wrist-01"]["status"] == "online"
    assert snap["rlgl/status/station-01"]["status"] == "offline"


# ---------- รอยต่อไฟแดง -> ไฟเขียว: ค่าจากข้อมือกับ state มาจากคนละอุปกรณ์ ลำดับที่มาถึงจึงสลับกันได้ ----------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def timed(*steps):
    """steps = (วินาทีที่ผ่านไปก่อนข้อความนี้, ข้อความ) คืน summary ของรอบ"""
    clock = Clock()
    tracker = GameTracker(clock=clock)
    out = []
    for wait, message in steps:
        clock.now += wait
        out.extend(tracker.handle(*message).outgoing)
    (_, summary), = out
    return summary


def test_walking_sample_that_beats_the_green_light_state_is_not_counted():
    summary = timed(
        (0, state("RED_LIGHT")), (0.5, imu(0.05)), (0.5, imu(0.08)),
        (1.0, imu(0.354)), (0.003, state("GREEN_LIGHT")),
        (1.0, result(outcome="WIN", reason="touch")),
    )
    assert summary["red_acc_peak_max"] == 0.08


def test_sample_older_than_the_margin_still_counts_when_light_turns_green():
    summary = timed(
        (0, state("RED_LIGHT")), (0.5, imu(0.25)), (0.25, state("GREEN_LIGHT")), (1.0, result()),
    )
    assert summary["red_acc_peak_max"] == 0.25


def test_motion_that_caused_the_loss_is_kept_even_if_it_is_the_last_sample():
    summary = timed(
        (0, state("RED_LIGHT")), (1.0, imu(0.05)), (0.05, imu(0.9)),
        (0.01, state("GAME_OVER")), (0.01, result(reason="imu")),
    )
    assert summary["red_acc_peak_max"] == 0.9


def test_result_arriving_before_the_final_state_keeps_the_last_sample():
    summary = timed((0, state("RED_LIGHT")), (1.0, imu(0.7)), (0.01, result(reason="imu")))
    assert summary["red_acc_peak_max"] == 0.7


def test_each_red_period_is_trimmed_on_its_own():
    summary = timed(
        (0, state("RED_LIGHT")), (1.0, imu(0.1)), (1.0, imu(0.4)), (0.05, state("GREEN_LIGHT")),
        (3.0, state("TRANSITION")), (1.0, state("RED_LIGHT")),
        (0.5, imu(0.06)), (1.0, imu(0.5)), (0.1, state("GREEN_LIGHT")),
        (1.0, result(outcome="WIN", reason="touch")),
    )
    assert summary["red_acc_peak_max"] == 0.1


def test_value_is_left_out_if_every_red_sample_was_at_the_boundary():
    summary = timed(
        (0, state("RED_LIGHT")), (1.0, imu(0.4)), (0.02, state("GREEN_LIGHT")), (1.0, result()),
    )
    assert "red_acc_peak_max" not in summary


# ---------- ข้อมูลที่หน้าประวัติและหน้าผลต้องใช้ ----------

def distance(cm):
    return "rlgl/gateway/distance", {"distance_cm": cm}


@pytest.mark.parametrize("last_state", ["GREEN_LIGHT", "TRANSITION"])
@pytest.mark.parametrize("final_state_first", [True, False])
def test_summary_says_in_which_light_the_finish_was_touched(last_state, final_state_first):
    ending = [state("WIN"), result(outcome="WIN", reason="touch")]
    if not final_state_first:
        ending.reverse()
    out = feed(GameTracker(), state("RED_LIGHT"), state("GREEN_LIGHT"), state(last_state), *ending)
    assert out[0][1]["finish_state"] == last_state


def test_touching_the_finish_on_red_is_a_finish_state_too():
    out = feed(GameTracker(), state("RED_LIGHT"), state("GAME_OVER"), result(reason="finish_during_red"))
    assert out[0][1]["finish_state"] == "RED_LIGHT"


def test_a_loss_without_touching_the_finish_has_no_finish_state():
    out = feed(GameTracker(), state("RED_LIGHT"), result(reason="camera"))
    assert "finish_state" not in out[0][1]


def test_summary_keeps_the_last_distance_measured_while_playing():
    out = feed(GameTracker(), state("CALIBRATE"), distance(400), state("GREEN_LIGHT"), distance(220),
               distance("far"), state("RED_LIGHT"), distance(190.5), state("GAME_OVER"), distance(10), result())
    assert out[0][1]["distance_cm"] == 190.5


def test_closest_call_is_the_highest_score_ratio_on_red_only():
    def ratio(value):
        return "rlgl/gateway/vision", {"motion_score": 0.01, "score_ratio": value}

    out = feed(GameTracker(), state("GREEN_LIGHT"), ratio(9.0), state("RED_LIGHT"), ratio(0.4),
               ratio(0.74), ratio(0.2), result(outcome="WIN", reason="touch"))
    assert out[0][1]["closest_call"] == 0.74


def test_the_pi_field_names_score_and_raw_are_used_too():
    # pi_client.py ส่ง score (เทียบเกณฑ์, 1.0 = ถึงเกณฑ์) และ raw (ระยะเลื่อน / ความยาวลำตัว)
    def frame(score, raw):
        return "rlgl/gateway/vision", {"event": "frame", "score": score, "raw": raw, "valid": True}

    out = feed(GameTracker(), state("GREEN_LIGHT"), frame(5.0, 0.9), state("RED_LIGHT"),
               frame(0.31, 0.012), frame(0.66, 0.027), frame(None, None),
               ("rlgl/gateway/vision", {"event": "heartbeat", "fps": 14.8}), result())
    summary = out[0][1]
    assert summary["closest_call"] == 0.66
    assert summary["red_motion_max"] == 0.027


def test_distance_and_closest_call_are_left_out_without_data():
    summary = feed(GameTracker(), state("RED_LIGHT"), vision(0.2), result())[0][1]
    assert "distance_cm" not in summary and "closest_call" not in summary
