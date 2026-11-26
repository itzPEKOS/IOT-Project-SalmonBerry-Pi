import pytest

from backend.store import Store, clean_name


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        self.now += 1
        return self.now


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "data" / "rlgl.db", clock=Clock())
    yield db
    db.close()


def finished(store, round_id, name, result="WIN", reason="touch", duration=20.0, **extra):
    player = store.select_player(name)["id"] if name else None
    store.start_round(round_id, player)
    store.finish_round({"round_id": round_id, "result": result, "reason": reason,
                        "duration_s": duration, **extra})


# ---------- ชื่อผู้เล่น ----------

@pytest.mark.parametrize("raw, name", [("berry", "BERRY"), ("  Nong-A ", "NONG-A"),
                                       ("pixel_p", "PIXEL_P"), ("a" * 10, "A" * 10)])
def test_names_are_trimmed_and_upper_cased(raw, name):
    assert clean_name(raw) == name


@pytest.mark.parametrize("raw", ["", "   ", "a" * 11, "two words", "<b>", "เบอร์รี่", None, 7])
def test_names_the_pixel_screen_cannot_show_are_refused(raw):
    assert clean_name(raw) is None


def test_the_same_name_in_any_case_is_one_player(store):
    first = store.select_player("BERRY")
    again = store.select_player("berry")
    assert first["id"] == again["id"]
    assert [p["name"] for p in store.recent_players()] == ["BERRY"]


def test_recent_players_are_newest_first_and_limited(store):
    for name in ["A", "B", "C", "D", "E", "B"]:
        store.select_player(name)
    assert [p["name"] for p in store.recent_players(limit=4)] == ["B", "E", "D", "C"]


# ---------- รอบการเล่น ----------

def test_a_finished_round_is_listed_with_its_player_and_cause(store):
    finished(store, "r1", "BERRY", result="GAME_OVER", reason="camera", duration=24.8,
             caught_part="right_arm", caught_red_s=3.8, caught_score_ratio=2.37, bpm_rest=76)
    row = store.round("r1")
    assert (row["number"], row["player"], row["result"], row["reason"]) == (1, "BERRY", "GAME_OVER", "camera")
    assert (row["duration_s"], row["caught_part"], row["caught_red_s"]) == (24.8, "right_arm", 3.8)
    assert (row["caught_score_ratio"], row["bpm_rest"]) == (2.37, 76)
    assert row["ended_at"] > row["started_at"] > 1_000_000  # epoch ms จากนาฬิกาของ laptop


def test_a_repeated_result_does_not_overwrite_the_first(store):
    finished(store, "r1", "BERRY", duration=18.0)
    before = store.round("r1")
    store.finish_round({"round_id": "r1", "result": "GAME_OVER", "reason": "imu", "duration_s": 99})
    assert store.round("r1") == before


def test_starting_the_same_round_again_keeps_the_first_player(store):
    store.start_round("r1", store.select_player("BERRY")["id"])
    store.start_round("r1", store.select_player("SALMON")["id"])
    assert store.round("r1")["player"] == "BERRY"


def test_player_can_be_changed_only_before_the_result(store):
    store.start_round("r1", None)
    store.assign_player("r1", store.select_player("BERRY")["id"])
    assert store.round("r1")["player"] == "BERRY"
    store.finish_round({"round_id": "r1", "result": "WIN", "reason": "touch", "duration_s": 10})
    store.assign_player("r1", store.select_player("SALMON")["id"])
    assert store.round("r1")["player"] == "BERRY"


def test_a_result_for_a_round_that_was_never_started_is_still_kept(store):
    store.finish_round({"round_id": "r9", "result": "GAME_OVER", "reason": "timeout", "duration_s": 60})
    assert (store.round("r9")["player"], store.round("r9")["reason"]) == (None, "timeout")


@pytest.mark.parametrize("payload", [
    {"round_id": "r1", "result": "MAYBE"},
    {"round_id": "../x", "result": "WIN"},
    {"round_id": ["r1"], "result": "WIN"},
    {"result": "WIN"},
])
def test_a_malformed_result_is_ignored(store, payload):
    store.finish_round(payload)
    assert store.history()["total"] == 0


def test_wrong_value_types_are_stored_as_empty_not_crashing(store):
    store.finish_round({"round_id": "r1", "result": "WIN", "reason": {"x": 1}, "duration_s": "fast",
                        "caught_part": 5, "caught_red_s": True, "bpm_rest": [76]})
    row = store.round("r1")
    assert [row[k] for k in ("reason", "duration_s", "caught_part", "caught_red_s", "bpm_rest")] == [None] * 5


def test_summary_and_clip_are_added_to_the_round(store):
    finished(store, "r1", "BERRY")
    store.save_summary({"round_id": "r1", "bpm_avg": 104.2, "bpm_max": 131, "red_light_count": 4,
                        "red_motion_max": 0.02, "red_acc_peak_max": 0.08, "closest_call": 0.74,
                        "distance_cm": 48.0, "finish_state": "TRANSITION", "device_id": "backend-01"})
    store.set_clip("r1", "r1.mp4")
    row = store.round("r1")
    assert (row["bpm_avg"], row["bpm_max"], row["red_light_count"], row["closest_call"]) == (104.2, 131, 4, 0.74)
    assert (row["distance_cm"], row["finish_state"], row["clip_file"]) == (48.0, "TRANSITION", "r1.mp4")


def test_unknown_round_is_none(store):
    assert store.round("nope") is None


# ---------- ประวัติ ----------

def test_history_shows_only_wins_and_losses_newest_first(store):
    finished(store, "r1", "BERRY", result="WIN")
    finished(store, "r2", "SALMON", result="GAME_OVER", reason="camera")
    finished(store, "r3", "TOON", result="ABORTED", reason="disconnect")
    store.start_round("r4", None)  # ยังเล่นไม่จบ
    page = store.history()
    assert [r["round_id"] for r in page["rounds"]] == ["r2", "r1"]
    assert page["counts"] == {"all": 2, "wins": 1, "losses": 1}
    assert page["last_ended_at"] == store.round("r2")["ended_at"]


def test_history_filters_and_pages(store):
    for number in range(1, 10):
        finished(store, f"r{number}", "BERRY", result="WIN" if number % 3 == 0 else "GAME_OVER",
                 reason="touch" if number % 3 == 0 else "imu")
    wins = store.history(result="win")
    assert [r["round_id"] for r in wins["rounds"]] == ["r9", "r6", "r3"]
    assert (wins["total"], wins["counts"]["all"]) == (3, 9)
    second = store.history(result="loss", page=2, page_size=4)
    assert [r["round_id"] for r in second["rounds"]] == ["r2", "r1"]
    assert second["total"] == 6


def test_empty_history(store):
    page = store.history()
    assert (page["rounds"], page["total"], page["last_ended_at"]) == ([], 0, None)


# ---------- อันดับและสถิติ ----------

def test_leaderboard_is_each_named_players_best_win(store):
    finished(store, "r1", "BERRY", duration=30.0)
    finished(store, "r2", "BERRY", duration=24.1)
    finished(store, "r3", "SALMON", duration=18.6)
    finished(store, "r4", "TOON", result="GAME_OVER", reason="camera", duration=5.0)
    finished(store, "r5", None, duration=1.0)  # ไม่มีชื่อ ไม่ขึ้นอันดับ
    store.save_summary({"round_id": "r2", "bpm_max": 131})
    board = store.leaderboard()
    assert [(e["rank"], e["player"], e["duration_s"]) for e in board] == [(1, "SALMON", 18.6), (2, "BERRY", 24.1)]
    assert (board[1]["bpm_max"], board[1]["round_id"]) == (131, "r2")


def test_equal_times_rank_the_earlier_win_first(store):
    finished(store, "r1", "BERRY", duration=20.0)
    finished(store, "r2", "SALMON", duration=20.0)
    assert [e["player"] for e in store.leaderboard()] == ["BERRY", "SALMON"]


def test_leaderboard_limit(store):
    for number in range(10):
        finished(store, f"r{number}", f"P{number}", duration=10 + number)
    assert [e["player"] for e in store.leaderboard(limit=3)] == ["P0", "P1", "P2"]


def test_player_stats_for_the_welcome_back_box(store):
    finished(store, "r1", "SALMON", duration=18.6)
    finished(store, "r2", "BERRY", duration=24.1)
    finished(store, "r3", "BERRY", result="GAME_OVER", reason="imu")
    finished(store, "r4", "BERRY", result="ABORTED", reason="disconnect")
    stats = store.player_stats("berry")
    assert stats == {"name": "BERRY", "rounds_played": 2, "wins": 1, "best_s": 24.1, "rank": 2}


def test_stats_of_a_new_player_and_an_unknown_player(store):
    store.select_player("NEW")
    assert store.player_stats("NEW") == {"name": "NEW", "rounds_played": 0, "wins": 0,
                                         "best_s": None, "rank": None}
    assert store.player_stats("NOBODY") is None


def test_a_new_record_names_who_was_beaten_and_by_how_much(store):
    finished(store, "r1", "SALMON", duration=18.6)
    finished(store, "r2", "BERRY", duration=17.9)
    assert store.standing("r2") == {"rank": 1, "personal_best": True, "new_record": True,
                                    "beaten": {"player": "SALMON", "by_s": 0.7}}


def test_standing_describes_the_moment_the_round_ended(store):
    finished(store, "r1", "SALMON", duration=18.6)
    finished(store, "r2", "BERRY", duration=25.0)
    finished(store, "r3", "BERRY", duration=17.0)
    assert store.standing("r1") == {"rank": 1, "personal_best": True, "new_record": True, "beaten": None}
    assert store.standing("r2") == {"rank": 2, "personal_best": True, "new_record": False, "beaten": None}


def test_no_standing_for_a_loss_or_a_guest_win(store):
    finished(store, "r1", "BERRY", result="GAME_OVER", reason="imu")
    finished(store, "r2", None, duration=3.0)
    finished(store, "r3", "SALMON", duration=18.6)
    assert store.standing("r1") is None and store.standing("r2") is None
    assert store.standing("r3")["new_record"] is True  # คะแนนของ guest ไม่นับ


# ---------- อยู่ต่อหลังปิดโปรแกรม ----------

def test_data_survives_reopening_the_file(tmp_path):
    path = tmp_path / "rlgl.db"
    first = Store(path)
    finished(first, "r1", "BERRY")
    first.close()
    second = Store(path)
    assert second.round("r1")["player"] == "BERRY"
    second.close()


def test_next_round_number_is_one_more_than_the_last(store):
    assert store.next_number() == 1
    finished(store, "r1", "BERRY")
    store.start_round("r1", None)  # ข้อความซ้ำไม่ทำให้เลขกระโดด
    assert store.next_number() == 2
    store.start_round("r2", None)
    assert (store.round("r2")["number"], store.next_number()) == (2, 3)
