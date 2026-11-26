import logging
import sqlite3

from backend.app import Settings
from test_api import inject, make, upload


def choose(client, name):
    return client.post("/api/session", json={"name": name})


def state(client, name, round_id="r1"):
    inject(client, "rlgl/game/state", {"round_id": round_id, "state": name, "remaining_s": 30})


def result(client, round_id="r1", outcome="WIN", reason="touch", duration=20.0):
    inject(client, "rlgl/game/result",
           {"round_id": round_id, "result": outcome, "reason": reason, "duration_s": duration})


def play(client, round_id="r1", **ending):
    state(client, "CALIBRATE", round_id)
    state(client, "GREEN_LIGHT", round_id)
    state(client, "RED_LIGHT", round_id)
    result(client, round_id, **ending)


def detail(client, round_id="r1"):
    return client.get(f"/api/rounds/{round_id}")


# ---------- เลือกชื่อผู้เล่น ----------

def test_choosing_a_name_returns_the_welcome_back_numbers(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert client.get("/api/session").json() == {"player": None, "next_round_number": 1, "mode": "rlgl"}
        body = choose(client, "berry").json()
        assert body == {"player": {"name": "BERRY", "rounds_played": 0, "wins": 0,
                                   "best_s": None, "rank": None},
                        "next_round_number": 1, "mode": "rlgl"}
        play(client)
        again = client.get("/api/session").json()
        assert (again["player"]["rounds_played"], again["player"]["rank"], again["next_round_number"]) == (1, 1, 2)


def test_a_bad_name_is_refused_and_keeps_the_current_player(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        for body in ({"name": "much too long a name"}, {"name": ""}, {"name": 5}, {}):
            assert client.post("/api/session", json=body).status_code == 422, body
        assert client.get("/api/session").json()["player"]["name"] == "BERRY"


def test_a_numeric_round_id_from_the_game_engine_is_saved_as_text(tmp_path):
    # game engine ใช้ round_id เป็นตัวเลข เพราะ pi_client.py จัดรูปแบบด้วย :03d
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        play(client, round_id=1795686400)
        body = detail(client, "1795686400").json()
        assert body["round_id"] == "1795686400"
        assert body["player"] == "BERRY"
        assert body["result"] == "WIN"


def test_clearing_the_name_makes_the_next_round_a_guest_round(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        assert client.delete("/api/session").json()["player"] is None
        play(client)
        assert detail(client).json()["player"] is None


# ---------- บันทึกรอบจากข้อความ MQTT ----------

def test_a_played_round_is_saved_with_name_summary_and_standing(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        state(client, "CALIBRATE")
        state(client, "GREEN_LIGHT")
        inject(client, "rlgl/wrist/heartrate", {"bpm": 100})
        inject(client, "rlgl/gateway/distance", {"distance_cm": 55})
        inject(client, "rlgl/wrist/heartrate", {"bpm": 120})
        result(client, duration=17.9)
        saved = detail(client).json()
        assert (saved["number"], saved["player"], saved["result"], saved["duration_s"]) == (1, "BERRY", "WIN", 17.9)
        assert (saved["bpm_avg"], saved["bpm_max"], saved["distance_cm"]) == (110.0, 120, 55)
        assert saved["finish_state"] == "GREEN_LIGHT"
        assert saved["standing"] == {"rank": 1, "personal_best": True, "new_record": True, "beaten": None}
        assert saved["video_url"] is None and "clip_file" not in saved


def test_the_video_link_appears_after_the_upload(tmp_path):
    client, _ = make(tmp_path)
    with client:
        play(client)
        upload(client, "r1")
        assert detail(client).json()["video_url"] == "http://testserver/clips/r1.mp4"
        assert client.get("/api/rounds").json()["rounds"][0]["video_url"] == "http://testserver/clips/r1.mp4"


def test_no_video_link_when_the_file_has_been_removed(tmp_path):
    client, _ = make(tmp_path)
    with client:
        play(client)
        upload(client, "r1")
        (tmp_path / "clips" / "r1.mp4").unlink()
        assert detail(client).json()["video_url"] is None


def test_a_name_chosen_during_calibration_applies_to_that_round(tmp_path):
    client, _ = make(tmp_path)
    with client:
        state(client, "CALIBRATE")
        choose(client, "berry")
        state(client, "GREEN_LIGHT")
        result(client)
        assert detail(client).json()["player"] == "BERRY"


def test_a_name_chosen_while_playing_waits_for_the_next_round(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        state(client, "CALIBRATE")
        state(client, "GREEN_LIGHT")
        choose(client, "salmon")
        result(client)
        play(client, "r2")
        assert [detail(client, r).json()["player"] for r in ("r1", "r2")] == ["BERRY", "SALMON"]


def test_a_round_that_skips_calibration_is_still_saved(tmp_path):
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        state(client, "GREEN_LIGHT")
        result(client)
        assert detail(client).json()["player"] == "BERRY"


def test_replayed_old_states_do_not_create_rounds_for_the_current_player(tmp_path):
    """rlgl/game/state เป็น retained: ต่อ broker ใหม่จะได้ state สุดท้ายของรอบเก่าซ้ำ"""
    client, _ = make(tmp_path)
    with client:
        choose(client, "berry")
        for name in ("WIN", "GAME_OVER", "IDLE"):
            state(client, name, f"old-{name}")
            assert detail(client, f"old-{name}").status_code == 404
        assert client.get("/api/session").json()["next_round_number"] == 1


def test_rounds_survive_a_restart_and_keep_their_player(tmp_path):
    database = tmp_path / "data" / "rlgl.db"
    client, _ = make(tmp_path, db_path=database)
    with client:
        choose(client, "berry")
        state(client, "RED_LIGHT")
    client, _ = make(tmp_path, db_path=database)
    with client:
        choose(client, "salmon")
        state(client, "RED_LIGHT")  # broker ส่ง state เดิมซ้ำหลัง backend เปิดใหม่
        result(client, outcome="GAME_OVER", reason="imu")
        assert (detail(client).json()["player"], detail(client).json()["reason"]) == ("BERRY", "imu")


def test_a_reused_round_id_is_reported(tmp_path, caplog):
    client, _ = make(tmp_path)
    with client, caplog.at_level(logging.WARNING):
        play(client)
        play(client, "r2")
        state(client, "CALIBRATE", "r1")
    assert "r1" in caplog.text and "already has a result" in caplog.text


def test_a_database_error_never_stops_the_player_screen(tmp_path, monkeypatch):
    client, _ = make(tmp_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with client:
        store = client.app.state.store
        with monkeypatch.context() as patch:
            patch.setattr(store, "start_round", broken)
            patch.setattr(store, "finish_round", broken)
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                state(client, "RED_LIGHT")
                result(client, outcome="GAME_OVER", reason="imu")
                topics = [ws.receive_json()["topic"] for _ in range(3)]
        assert topics == ["rlgl/game/state", "rlgl/game/result", "rlgl/game/summary"]
        state(client, "RED_LIGHT", "r2")  # ฐานข้อมูลกลับมาแล้ว รอบต่อไปต้องบันทึกได้
        assert detail(client, "r2").status_code == 200


def test_a_failed_save_of_a_new_round_is_retried_on_the_next_state(tmp_path, monkeypatch):
    client, _ = make(tmp_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with client:
        choose(client, "berry")
        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.store, "start_round", broken)
            state(client, "CALIBRATE")
        state(client, "CALIBRATE")
        assert detail(client).json()["player"] == "BERRY"


# ---------- หน้าอันดับ ประวัติ และผู้เล่น ----------

def test_leaderboard_history_and_player_endpoints(tmp_path):
    client, _ = make(tmp_path)
    with client:
        for number, (name, outcome, reason, seconds) in enumerate([
                ("berry", "WIN", "touch", 24.1), ("salmon", "WIN", "touch", 18.6),
                ("berry", "GAME_OVER", "camera", 30.0)], start=1):
            choose(client, name)
            play(client, f"r{number}", outcome=outcome, reason=reason, duration=seconds)

        board = client.get("/api/leaderboard").json()["entries"]
        assert [(e["rank"], e["player"]) for e in board] == [(1, "SALMON"), (2, "BERRY")]
        assert len(client.get("/api/leaderboard?limit=1").json()["entries"]) == 1

        history = client.get("/api/rounds?result=loss").json()
        assert [r["round_id"] for r in history["rounds"]] == ["r3"]
        assert history["counts"] == {"all": 3, "wins": 2, "losses": 1}
        assert [r["round_id"] for r in client.get("/api/rounds?page=2&page_size=2").json()["rounds"]] == ["r1"]

        assert client.get("/api/players?limit=1").json() == {"players": [{"name": "BERRY"}]}
        assert client.get("/api/players/Salmon").json()["rank"] == 1
        assert client.get("/api/players/nobody").status_code == 404
        assert detail(client, "nope").status_code == 404


def test_query_values_out_of_range_are_refused(tmp_path):
    client, _ = make(tmp_path)
    with client:
        for url in ("/api/rounds?result=draw", "/api/rounds?page=0", "/api/rounds?page=100001",
                    "/api/rounds?page_size=0", "/api/rounds?page_size=51",
                    "/api/leaderboard?limit=0", "/api/leaderboard?limit=51", "/api/players?limit=-1"):
            assert client.get(url).status_code == 422, url


def test_a_configured_dev_server_may_also_send_the_player_name(tmp_path):
    origin = "http://localhost:5173"
    client, _ = make(tmp_path, cors_origins=(origin,))
    with client:
        for method in ("POST", "DELETE"):
            response = client.options("/api/session", headers={
                "Origin": origin, "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type"})
            assert response.status_code == 200, method
            assert method in response.headers["access-control-allow-methods"], method


# ---------- ที่เก็บไฟล์ฐานข้อมูล ----------

def test_database_file_location_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("RLGL_DB_PATH", raising=False)
    default = Settings.from_env().db_path
    assert (default.name, default.parent.name, default.parent.parent.name) == ("rlgl.db", "data", "web")
    monkeypatch.setenv("RLGL_DB_PATH", str(tmp_path / "other.db"))
    assert Settings.from_env().db_path == tmp_path / "other.db"


def test_tests_never_touch_the_real_database():
    assert Settings().db_path is None
