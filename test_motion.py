"""
ทดสอบตรวจการขยับตอนไฟแดงด้วยเว็บแคม (Red Light, Green Light)

ติดตั้ง:
    pip install ultralytics opencv-python

รัน:
    python test_motion.py                    # YOLO26n-pose (เทียบตำแหน่ง keypoint)
    python test_motion.py --method diff      # Frame differencing (baseline ไว้เทียบใน evaluation)
    python test_motion.py --thr 0.04         # ปรับความไวของการจับ

ปุ่ม:
    SPACE = สลับไฟเขียว / แดง (ไฟแดงมี grace period ก่อนเริ่มจับ)
    s     = เซฟภาพหน้าจอไว้ทำ qualitative evaluation (โฟลเดอร์ captures/)
    q     = ออก
"""
import argparse
import os
import time

import cv2
import numpy as np

# COCO 17 keypoints -> แบ่งเป็นส่วนของร่างกาย เพื่อบอกได้ว่าขยับส่วนไหน
PARTS = {
    "head": [0, 1, 2, 3, 4],
    "left_arm": [5, 7, 9],
    "right_arm": [6, 8, 10],
    "torso": [5, 6, 11, 12],
    "left_leg": [11, 13, 15],
    "right_leg": [12, 14, 16],
}


def largest_person(r):
    """คืน (keypoints xy, conf, ความสูง bbox) ของคนที่ตัวใหญ่ที่สุดในภาพ"""
    if r.keypoints is None or r.boxes is None or len(r.boxes) == 0:
        return None
    boxes = r.boxes.xyxy.cpu().numpy()
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    i = int(areas.argmax())
    xy = r.keypoints.xy[i].cpu().numpy()
    conf = r.keypoints.conf[i].cpu().numpy() if r.keypoints.conf is not None else np.ones(len(xy))
    return xy, conf, float(boxes[i, 3] - boxes[i, 1])


def pose_score(ref, cur, min_conf=0.5):
    """ระยะที่ keypoint เลื่อนไปจากตอนเริ่มไฟแดง หารด้วยความสูงตัว (ไม่ขึ้นกับระยะห่างจากกล้อง)
    คืน (คะแนนของส่วนที่ขยับมากที่สุด, ชื่อส่วนนั้น)"""
    (xy0, c0, h0), (xy1, c1, h1) = ref, cur
    h = max((h0 + h1) / 2, 1.0)
    ok = (c0 > min_conf) & (c1 > min_conf)
    d = np.linalg.norm(xy1 - xy0, axis=1) / h
    parts = {}
    for name, idx in PARTS.items():
        idx = [k for k in idx if ok[k]]
        if idx:
            parts[name] = float(d[idx].mean())
    if not parts:
        return None, None
    worst = max(parts, key=parts.get)
    return parts[worst], worst


def diff_prep(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (21, 21), 0)


def diff_score(ref, cur):
    """สัดส่วนพิกเซลที่เปลี่ยนไปจากภาพตอนเริ่มไฟแดง"""
    d = cv2.absdiff(ref, cur)
    _, mask = cv2.threshold(d, 25, 255, cv2.THRESH_BINARY)
    mask = cv2.dilate(mask, None, iterations=2)
    return float(np.count_nonzero(mask)) / mask.size, mask


def put(img, text, y, color=(255, 255, 255), scale=0.7):
    cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=["pose", "diff"], default="pose")
    ap.add_argument("--model", default="yolo26n-pose.pt", help="ถ้าโหลดไม่ได้ลอง yolo11n-pose.pt")
    ap.add_argument("--imgsz", type=int, default=320, help="320 เร็ว, 640 แม่นกว่า")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--thr", type=float, default=None, help="default: pose 0.05, diff 0.02")
    ap.add_argument("--frames", type=int, default=3, help="ต้องเกิน thr ติดกันกี่เฟรมถึงนับว่าขยับ")
    ap.add_argument("--grace", type=float, default=0.7, help="วินาทีหลังไฟแดงที่ยังไม่จับ")
    args = ap.parse_args()
    thr = args.thr if args.thr is not None else (0.05 if args.method == "pose" else 0.02)

    model = None
    if args.method == "pose":
        from ultralytics import YOLO
        model = YOLO(args.model)

    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    cap = cv2.VideoCapture(args.cam, backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        raise SystemExit("เปิดกล้องไม่ได้ ลองเปลี่ยน --cam 1")

    os.makedirs("captures", exist_ok=True)
    state, t_red, ref, streak = "GREEN", 0.0, None, 0
    result, fps, t_prev = "", 0.0, time.perf_counter()

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        # --- inference ---
        mask = None
        if model is not None:
            r = model(frame, imgsz=args.imgsz, verbose=False)[0]
            vis = r.plot(boxes=False)
            cur = largest_person(r)
        else:
            vis = frame.copy()
            cur = diff_prep(frame)

        # --- state machine แบบย่อ ---
        now = time.time()
        score, part = None, None
        if state == "GRACE" and now - t_red >= args.grace and cur is not None:
            ref, state, streak = cur, "RED", 0
        elif state == "RED" and cur is not None:
            if model is not None:
                score, part = pose_score(ref, cur)
            else:
                score, mask = diff_score(ref, cur)
            streak = streak + 1 if (score is not None and score > thr) else 0
            if streak >= args.frames:
                state = "CAUGHT"
                result = f"MOVED: {part}  score={score:.3f}" if part else f"MOVED  score={score:.3f}"
                print(result)

        if mask is not None:
            vis[mask > 0] = (0.5 * vis[mask > 0] + [0, 0, 127]).astype(np.uint8)

        # --- FPS (ทั้ง loop รวมอ่านกล้อง) ---
        t = time.perf_counter()
        fps = 0.9 * fps + 0.1 * (1.0 / max(t - t_prev, 1e-6)) if fps else 1.0 / max(t - t_prev, 1e-6)
        t_prev = t

        # --- overlay ---
        colors = {"GREEN": (0, 220, 0), "GRACE": (0, 200, 255), "RED": (0, 0, 255), "CAUGHT": (0, 0, 255)}
        put(vis, f"{state}   {args.method}   FPS {fps:.1f}", 30, colors[state])
        if state == "RED":
            if cur is None or (model is not None and score is None):
                put(vis, "no player detected", 60, (0, 200, 255))
            else:
                put(vis, f"score {score:.3f} / thr {thr}" + (f"  [{part}]" if part else ""), 60)
        if state == "CAUGHT":
            put(vis, result, 60, (0, 0, 255))
        put(vis, "SPACE: green/red   s: save   q: quit", vis.shape[0] - 15, scale=0.5)

        cv2.imshow("RLGL motion test", vis)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord(" "):
            if state == "GREEN":
                state, t_red = "GRACE", now
            else:
                state, result = "GREEN", ""
        if key == ord("s"):
            path = os.path.join("captures", f"{args.method}_{state}_{int(now)}.jpg")
            cv2.imwrite(path, vis)
            print("saved", path)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
