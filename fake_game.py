"""
fake_game.py: Game Engine จำลอง ไว้ทดสอบ pi_client.py โดยไม่ต้องรอ engine จริง

ทำแค่นี้:
    ส่ง rlgl/game/state (QoS 1, retain)  CALIBRATE → GREEN_LIGHT → TRANSITION → RED_LIGHT → GREEN_LIGHT …
    ฟัง rlgl/gateway/vision  ถ้าได้ event="moved" ตอน RED_LIGHT → ส่ง GAME_OVER + rlgl/game/result แล้วเริ่มรอบใหม่
    ถ้านิ่งรอดไฟแดงครบ --win-after ครั้ง → ถือว่าเดินถึงเส้นชัย ส่ง WIN (ไว้ทดสอบคลิปตอนชนะ)
    ฟัง rlgl/gateway/replay  พิมพ์ลิงก์คลิปรีเพลย์ที่ Pi อัปโหลดเสร็จ
    ฟัง rlgl/status/#        พิมพ์ว่าอุปกรณ์ online / offline

รัน:  python fake_game.py                     (broker ที่ localhost)
      python fake_game.py --broker 192.168.137.1 --green 5 --red 5
"""
import argparse
import json
import time

import paho.mqtt.client as mqtt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--calib", type=float, default=5.0)
    ap.add_argument("--green", type=float, default=4.0)
    ap.add_argument("--transition", type=float, default=1.5, help="กล้องหัน + ช่วงผ่อนผัน (ยังไม่จับ)")
    ap.add_argument("--red", type=float, default=4.0)
    ap.add_argument("--over", type=float, default=3.0, help="ค้างหน้า GAME_OVER / WIN กี่วินาทีก่อนเริ่มรอบใหม่")
    ap.add_argument("--win-after", type=int, default=3, help="รอดไฟแดงกี่ครั้งถึงนับว่าชนะ (0 = ไม่มีชนะ)")
    args = ap.parse_args()

    seq = 0
    moved_event = {}

    def wrap(data):
        nonlocal seq
        seq += 1
        return json.dumps({"device_id": "game-sim", "seq": seq, "ts": int(time.time() * 1000), **data})

    def on_connect(c, *a):
        c.subscribe([("rlgl/gateway/vision", 1), ("rlgl/status/#", 1), ("rlgl/gateway/replay", 1)])
        print("connected to broker")

    last_print = [0.0]

    def on_message(c, u, msg):
        try:
            d = json.loads(msg.payload)
        except ValueError:
            return
        if msg.topic.startswith("rlgl/status/"):
            print(f"  [status] {msg.topic.split('/')[-1]}: {d.get('status')}")
            return
        if msg.topic == "rlgl/gateway/replay":
            print(f"  [replay] round {d.get('round_id')} {d.get('result')} → {d.get('page') or d.get('url')}")
            return
        ev = d.get("event")
        if ev == "moved":
            moved_event.update(d)
        elif ev == "calibrated":
            print(f"  [vision] calibrated: noise x{d.get('noise_factor')} ok={d.get('ok')}")
        elif ev == "frame" and time.time() - last_print[0] > 0.5:
            last_print[0] = time.time()
            print(f"  [vision] score={d.get('score')}  {d.get('part')}/{d.get('joint')}  valid={d.get('valid')}  fps={d.get('fps')}")

    try:
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="game-sim")
    except AttributeError:
        cli = mqtt.Client(client_id="game-sim")
    cli.on_connect, cli.on_message = on_connect, on_message
    cli.connect(args.broker, args.port, keepalive=10)
    cli.loop_start()

    def set_state(state, rid, **extra):
        cli.publish("rlgl/game/state", wrap({"state": state, "round_id": rid, **extra}), qos=1, retain=True)
        print(f"[game] round {rid}: {state}")

    rid = 1
    try:
        set_state("CALIBRATE", rid)
        time.sleep(args.calib)
        survived = 0
        while True:
            for state, dur in (("GREEN_LIGHT", args.green), ("TRANSITION", args.transition), ("RED_LIGHT", args.red)):
                moved_event.clear()
                set_state(state, rid, remaining_s=dur)
                t_end = time.time() + dur
                while time.time() < t_end:
                    if state == "RED_LIGHT" and moved_event:
                        break
                    time.sleep(0.05)
                if state == "RED_LIGHT" and moved_event:
                    res = {"result": "GAME_OVER", "cause": "camera", "part": moved_event.get("part"),
                           "joint": moved_event.get("joint"), "score": moved_event.get("score")}
                    cli.publish("rlgl/game/result", wrap({"round_id": rid, **res}), qos=1)
                    set_state("GAME_OVER", rid, **res)
                    print(f"[game] GAME OVER — {res['part']}/{res['joint']} score {res['score']}")
                    time.sleep(args.over)
                    rid += 1
                    survived = 0
                    break
                if state == "RED_LIGHT":
                    survived += 1
                    if args.win_after and survived >= args.win_after:
                        set_state("GREEN_LIGHT", rid, remaining_s=1.5)
                        time.sleep(1.5)  # เดินเข้าเส้นชัย
                        res = {"result": "WIN", "cause": "finish", "red_lights": survived}
                        cli.publish("rlgl/game/result", wrap({"round_id": rid, **res}), qos=1)
                        set_state("WIN", rid, **res)
                        print(f"[game] WIN — survived {survived} red lights")
                        time.sleep(args.over)
                        rid += 1
                        survived = 0
    except KeyboardInterrupt:
        pass
    set_state("IDLE", rid)
    time.sleep(0.3)
    cli.loop_stop()
    cli.disconnect()


if __name__ == "__main__":
    main()
