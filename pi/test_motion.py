"""
ทดสอบตรวจการขยับตอนไฟแดงด้วยเว็บแคม (Red Light, Green Light)

ติดตั้ง (ใน conda env):
    pip install ultralytics mediapipe opencv-python psutil

รัน:
    python test_motion.py                    # YOLO26n-pose (เทียบตำแหน่ง keypoint)
    python test_motion.py --method mediapipe # MediaPipe Pose Landmarker (lite) โหลดโมเดลให้อัตโนมัติ
    python test_motion.py --method mediapipe --mp-variant full   # รุ่นที่แม่นกว่าแต่ช้ากว่า
    python test_motion.py --method diff      # Frame differencing (baseline ไว้เทียบใน evaluation)
    python test_motion.py --thr 0.04         # ปรับความไวของการจับ (ตัวเดิม / diff)
    python test_motion.py --method mediapipe --detector old   # ตัวตรวจแบบเดิม (ค่าเริ่มต้นคือตัวใหม่ใน motion_detector.py)
    python test_motion.py --method mediapipe --record --label still   # บันทึก keypoint ไว้ประเมินด้วย eval_motion.py

ตัวใหม่ (pose/mediapipe): เริ่มโปรแกรมจะมีช่วง CALIB ให้ยืนนิ่ง --calib วินาที (กด c เพื่อทำใหม่)

ปุ่ม:
    SPACE = สลับไฟเขียว / แดง (ไฟแดงมี grace period ก่อนเริ่มจับ)
    s     = เซฟภาพหน้าจอไว้ทำ qualitative evaluation (โฟลเดอร์ captures/)
    n     = (mediapipe) เปิด/ปิดเลขกำกับจุด 0–32
    c     = (ตัวใหม่) calibrate ใหม่: ยืนนิ่ง --calib วินาที
    + / - = เพิ่ม/ลด threshold ระหว่างรัน (ตัวใหม่ k ทีละ 0.25, ตัวเดิม ทีละ 0.005, diff ทีละ 0.002)
    ] / [ = (ตัวเดิม / diff) เพิ่ม/ลด จำนวนเฟรมติดกันที่ต้องเกิน threshold
    q     = ออก (พิมพ์สรุป FPS / CPU / RAM และบันทึกต่อท้าย captures/benchmark.csv)
"""
import argparse
import csv
import os
import platform
import time

import cv2
import numpy as np

from motion_detector import DetectorConfig, MotionDetector, OldDetector

# COCO 17 keypoints -> แบ่งเป็นส่วนของร่างกาย เพื่อบอกได้ว่าขยับส่วนไหน
PARTS = {
    "head": [0, 1, 2, 3, 4],
    "left_arm": [5, 7, 9],
    "right_arm": [6, 8, 10],
    "torso": [5, 6, 11, 12],
    "left_leg": [11, 13, 15],
    "right_leg": [12, 14, 16],
}

# MediaPipe มี 33 landmarks -> เลือก 17 จุดที่ตรงกับ COCO เพื่อใช้ PARTS และ pose_score ชุดเดียวกับ YOLO
MP_TO_COCO = [0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]
MP_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
                "pose_landmarker_{v}/float16/1/pose_landmarker_{v}.task")

# เส้นเชื่อมโครงร่างทั้ง 33 จุดของ MediaPipe (BlazePose)
MP_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8), (9, 10),
    (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21), (17, 19),
    (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
    (27, 29), (28, 30), (29, 31), (30, 32), (27, 31), (28, 32),
]


# ---------------------------------------------------------------- resource monitor
class ResourceMonitor:
    """วัด CPU / RAM ของโปรเซสนี้ (และ GPU ถ้า YOLO รันบน CUDA) อัปเดตทุก 0.5 วินาที"""

    def __init__(self, use_torch=False):
        import psutil
        self.psutil = psutil
        self.proc = psutil.Process()
        self.ncpu = psutil.cpu_count() or 1
        self.proc.cpu_percent(None)
        psutil.cpu_percent(None)
        self.cpu = self.sys_cpu = 0.0
        self.ram = self.peak_ram = self.proc.memory_info().rss / 2**20
        self.gpu = None
        self.cpu_samples = []
        self.t_last = time.perf_counter()
        self.torch = None
        if use_torch:
            try:
                import torch
                if torch.cuda.is_available():
                    self.torch = torch
            except Exception:
                pass

    def update(self):
        now = time.perf_counter()
        if now - self.t_last < 0.5:
            return
        self.t_last = now
        self.cpu = self.proc.cpu_percent(None) / self.ncpu   # % ของ CPU ทั้งเครื่อง
        self.sys_cpu = self.psutil.cpu_percent(None)
        self.ram = self.proc.memory_info().rss / 2**20
        self.peak_ram = max(self.peak_ram, self.ram)
        self.cpu_samples.append(self.cpu)
        if self.torch is not None:
            self.gpu = self.torch.cuda.memory_allocated() / 2**20

    def text(self):
        s = f"CPU {self.cpu:4.1f}% (sys {self.sys_cpu:4.1f}%)  RAM {self.ram:5.0f} MB"
        if self.gpu is not None:
            s += f"  GPU {self.gpu:4.0f} MB"
        return s


# ---------------------------------------------------------------- MediaPipe
class MediaPipePose:
    """MediaPipe Pose Landmarker (Tasks API, VIDEO mode) จับได้ทีละ 1 คน"""

    def __init__(self, variant="lite"):
        import mediapipe as mp
        self.mp = mp
        path = f"pose_landmarker_{variant}.task"
        if not os.path.exists(path) or os.path.getsize(path) < 100_000:
            import urllib.request
            url = MP_MODEL_URL.format(v=variant)
            print("downloading", url)
            try:
                urllib.request.urlretrieve(url, path)
            except Exception as e:
                if os.path.exists(path):
                    os.remove(path)
                raise SystemExit(f"โหลดโมเดลไม่สำเร็จ ({e})\n"
                                 f"ดาวน์โหลดเองจาก {url}\nแล้ววางไว้ที่ {os.path.abspath(path)}")
        opts = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=path),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=1,
        )
        self.landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(opts)
        self.t0, self.last_ts = time.perf_counter(), -1
        self.all_points = None  # (xy 33x2, visibility 33) ของเฟรมล่าสุด ไว้วาดทุกจุด

    def __call__(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        ts = max(int((time.perf_counter() - self.t0) * 1000), self.last_ts + 1)  # ต้องเพิ่มขึ้นทุกเฟรม
        self.last_ts = ts
        res = self.landmarker.detect_for_video(img, ts)
        if not res.pose_landmarks:
            self.all_points = None
            return None
        lms = res.pose_landmarks[0]
        h, w = frame.shape[:2]
        all_xy = np.array([[p.x * w, p.y * h] for p in lms], dtype=np.float32)
        all_vis = np.array([p.visibility or 0.0 for p in lms], dtype=np.float32)
        self.all_points = (all_xy, all_vis)
        xy, conf = all_xy[MP_TO_COCO], all_vis[MP_TO_COCO]
        ys = xy[conf > 0.5, 1]
        height = float(ys.max() - ys.min()) if len(ys) >= 2 else 1.0
        return xy, conf, height


def draw_mp_all(img, all_points, show_ids=False, min_vis=0.5):
    """วาดทั้ง 33 จุดพร้อมโครงร่าง: สีส้ม = 17 จุดที่ใช้คำนวณการขยับ, สีฟ้า = จุดอื่น (มือ เท้า หน้า)"""
    if all_points is None:
        return
    xy, vis = all_points
    pts = [(int(x), int(y)) for x, y in xy]
    for a, b in MP_CONNECTIONS:
        if vis[a] > min_vis and vis[b] > min_vis:
            cv2.line(img, pts[a], pts[b], (220, 220, 220), 2, cv2.LINE_AA)
    scored = set(MP_TO_COCO)
    for i, (p, v) in enumerate(zip(pts, vis)):
        if v <= min_vis:
            continue
        color = (0, 140, 255) if i in scored else (255, 200, 0)
        cv2.circle(img, p, 5 if i in scored else 3, color, -1, cv2.LINE_AA)
        if show_ids:
            cv2.putText(img, str(i), (p[0] + 4, p[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (255, 255, 255), 1, cv2.LINE_AA)


# ---------------------------------------------------------------- YOLO
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


# ---------------------------------------------------------------- scoring
def part_scores(ref, cur, min_conf=0.5):
    """คะแนนแยกทีละส่วนของร่างกาย: ระยะที่ keypoint เลื่อนไปจากตอนเริ่มไฟแดง หารด้วยความสูงตัว
    (ไม่ขึ้นกับระยะห่างจากกล้อง) ส่วนที่มองไม่เห็นจะไม่อยู่ใน dict"""
    (xy0, c0, h0), (xy1, c1, h1) = ref, cur
    h = max((h0 + h1) / 2, 1.0)
    ok = (c0 > min_conf) & (c1 > min_conf)
    d = np.linalg.norm(xy1 - xy0, axis=1) / h
    parts = {}
    for name, idx in PARTS.items():
        idx = [k for k in idx if ok[k]]
        if idx:
            parts[name] = float(d[idx].mean())
    return parts


def pose_score(ref, cur, min_conf=0.5):
    """คืน (คะแนนของส่วนที่ขยับมากที่สุด, ชื่อส่วนนั้น)"""
    parts = part_scores(ref, cur, min_conf)
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


def put(img, text, y, color=(255, 255, 255), scale=0.6):
    cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def draw_part_panel(img, parts, thr):
    """แถบคะแนนแยกตามส่วนของร่างกาย มุมขวาบน: แดง = เกิน thr"""
    x0, y0, bar_w = img.shape[1] - 230, 20, 120
    full = max(thr * 2, 1e-6)  # ความยาวเต็มแถบ = 2 เท่าของ thr
    for i, name in enumerate(PARTS):
        y = y0 + i * 22
        cv2.putText(img, name, (x0, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        bx = x0 + 80
        cv2.rectangle(img, (bx, y), (bx + bar_w, y + 14), (80, 80, 80), 1)
        cv2.line(img, (bx + bar_w // 2, y - 2), (bx + bar_w // 2, y + 16), (200, 200, 200), 1)  # เส้น thr
        v = parts.get(name)
        if v is None:
            cv2.putText(img, "-", (bx + 4, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (150, 150, 150), 1)
            continue
        w = int(min(v / full, 1.0) * bar_w)
        color = (0, 0, 255) if v > thr else (0, 200, 0)
        cv2.rectangle(img, (bx, y + 1), (bx + w, y + 13), color, -1)
        cv2.putText(img, f"{v:.3f}", (bx + bar_w + 6, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)


def save_benchmark(row):
    path = os.path.join("captures", "benchmark.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    print("appended", path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=["pose", "mediapipe", "diff"], default="pose")
    ap.add_argument("--model", default="yolo26n-pose.pt", help="ถ้าโหลดไม่ได้ลอง yolo11n-pose.pt")
    ap.add_argument("--mp-variant", choices=["lite", "full", "heavy"], default="lite")
    ap.add_argument("--imgsz", type=int, default=320, help="YOLO: 320 เร็ว, 640 แม่นกว่า")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--thr", type=float, default=None, help="default: pose/mediapipe 0.05, diff 0.02")
    ap.add_argument("--frames", type=int, default=3, help="ต้องเกิน thr ติดกันกี่เฟรมถึงนับว่าขยับ")
    ap.add_argument("--grace", type=float, default=0.7, help="วินาทีหลังไฟแดงที่ยังไม่จับ")
    ap.add_argument("--detector", choices=["new", "old"], default="new", help="ตัวตรวจสำหรับ pose/mediapipe")
    ap.add_argument("--k", type=float, default=4.5, help="ตัวใหม่: ความไว ยิ่งต่ำยิ่งไว")
    ap.add_argument("--calib", type=float, default=5.0, help="ตัวใหม่: วินาทีที่ยืนนิ่งตอนเริ่ม (0 = ข้าม)")
    ap.add_argument("--record", action="store_true", help="บันทึก keypoint ทุกเฟรมลง captures/rec_*.npz")
    ap.add_argument("--label", default="", help="still / moved / still,moved (สลับทีละรอบ)")
    args = ap.parse_args()
    thr = args.thr if args.thr is not None else (0.02 if args.method == "diff" else 0.05)
    uses_pose = args.method != "diff"
    det = None
    if uses_pose:
        det = (OldDetector(thr=thr, frames=args.frames) if args.detector == "old"
               else MotionDetector(DetectorConfig(k_sigma=args.k)))
    new_det = isinstance(det, MotionDetector)
    labels = [x.strip() for x in args.label.split(",") if x.strip()] or [""]
    rec = None
    if args.record and uses_pose:
        from eval_motion import PoseRecorder
        rec = PoseRecorder(os.path.join("captures", f"rec_{time.strftime('%Y%m%d_%H%M%S')}_"
                                        f"{args.label.replace(',', '-') or 'nolabel'}.npz"),
                           {"source": f"webcam{args.cam}", "method": args.method, "labels": labels})
    n_round = 0

    model, mp_pose = None, None
    if args.method == "pose":
        from ultralytics import YOLO
        model = YOLO(args.model)
        name = f"{os.path.splitext(os.path.basename(args.model))[0]}@{args.imgsz}"
    elif args.method == "mediapipe":
        mp_pose = MediaPipePose(args.mp_variant)
        name = f"mediapipe-{args.mp_variant}"
    else:
        name = "frame-diff"
    if uses_pose:
        name += f"+{args.detector}"

    mon = ResourceMonitor(use_torch=model is not None)
    ram_after_load = mon.ram

    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    cap = cv2.VideoCapture(args.cam, backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        raise SystemExit("เปิดกล้องไม่ได้ ลองเปลี่ยน --cam 1")

    os.makedirs("captures", exist_ok=True)
    state, t_red, ref, streak = "GREEN", 0.0, None, 0
    t_calib = None
    if new_det and args.calib > 0:
        state, t_calib = "CALIB", time.time()
        det.start_calibration()
        print(f"CALIBRATE: ยืนนิ่ง {args.calib:.0f} วินาที")
    result, fps, infer_ms = "", 0.0, 0.0
    infer_list, n_frames, t_start = [], 0, time.perf_counter()
    show_ids = False
    t_prev = t_start

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        # --- inference (จับเวลาเฉพาะส่วนโมเดล) ---
        mask = None
        t_inf = time.perf_counter()
        if model is not None:
            r = model(frame, imgsz=args.imgsz, verbose=False)[0]
            cur = largest_person(r)
            dt_inf = time.perf_counter() - t_inf
            vis = r.plot(boxes=False)
        elif mp_pose is not None:
            cur = mp_pose(frame)
            dt_inf = time.perf_counter() - t_inf
            vis = frame.copy()
            draw_mp_all(vis, mp_pose.all_points, show_ids)
        else:
            cur = diff_prep(frame)
            dt_inf = time.perf_counter() - t_inf
            vis = frame.copy()
        infer_list.append(dt_inf * 1000)
        infer_ms = 0.9 * infer_ms + 0.1 * dt_inf * 1000 if infer_ms else dt_inf * 1000

        # --- state machine แบบย่อ ---
        now = time.time()
        score, part = None, None
        if state == "CALIB" and now - t_calib >= args.calib:
            info = det.finish_calibration()
            print(f"calibrate: {info.get('frames')} เฟรม, noise x{info.get('noise_factor')}"
                  + (f"  ** {info['msg']}" if info.get("msg") else ""))
            state = "GREEN"
        if state == "GRACE" and now - t_red >= args.grace and cur is not None:
            ref, state, streak = cur, "RED", 0
            if det is not None:
                det.start_red()
        r = det.update(time.perf_counter(), cur) if det is not None else None
        if rec is not None:
            ph = {"CAUGHT": "RED"}.get(state, state)
            rec.add(time.perf_counter(), ph, n_round, labels[n_round % len(labels)] if ph == "RED" else "", cur)
        if state in ("RED", "CAUGHT") and r is not None:
            score, part = r.score, r.part
            if r.triggered:
                state = "CAUGHT"
                result = f"MOVED: {r.part}/{r.joint}  score={r.score:.2f}"
                print(result)
        elif state == "RED" and cur is not None:
            score, mask = diff_score(ref, cur)
            streak = streak + 1 if (score is not None and score > thr) else 0
            if streak >= args.frames:
                state = "CAUGHT"
                result = f"MOVED  score={score:.3f}"
                print(result)

        if mask is not None:
            vis[mask > 0] = (0.5 * vis[mask > 0] + [0, 0, 127]).astype(np.uint8)
        if new_det and state in ("RED", "CAUGHT") and det.mode == "judge":
            draw_part_panel(vis, det.last_parts, 1.0)           # คะแนนเทียบเกณฑ์ (1.0 = เกณฑ์)
        elif uses_pose and state in ("RED", "CAUGHT") and ref is not None and cur is not None:
            draw_part_panel(vis, part_scores(ref, cur), thr)  # หลังโดนจับยังแสดงต่อ ไว้ดูว่าขยับแค่ไหน

        # --- FPS (ทั้ง loop รวมอ่านกล้อง) + resource ---
        t = time.perf_counter()
        inst = 1.0 / max(t - t_prev, 1e-6)
        fps = 0.9 * fps + 0.1 * inst if fps else inst
        t_prev = t
        n_frames += 1
        mon.update()

        # --- overlay ---
        colors = {"GREEN": (0, 220, 0), "GRACE": (0, 200, 255), "RED": (0, 0, 255), "CAUGHT": (0, 0, 255),
                  "CALIB": (255, 200, 0)}
        put(vis, f"{state}   {name}   FPS {fps:4.1f}   infer {infer_ms:5.1f} ms", 28, colors[state])
        put(vis, mon.text(), 54, (220, 220, 220))
        if state == "CALIB":
            put(vis, f"CALIBRATE: stand still  {max(args.calib - (now - t_calib), 0):.1f}s", 80, (255, 200, 0))
        if state == "RED":
            if new_det and det.mode == "ref":
                put(vis, "building reference pose...", 80, (0, 200, 255))
            elif cur is None or (uses_pose and score is None):
                put(vis, "no player detected", 80, (0, 200, 255))
            elif new_det:
                put(vis, f"score {score:.2f} / 1.00  k={det.cfg.k_sigma}" + (f"  [{part}]" if part else ""), 80)
            else:
                put(vis, f"score {score:.3f} / thr {thr}" + (f"  [{part}]" if part else ""), 80)
        if state == "CAUGHT":
            put(vis, result, 80, (0, 0, 255))
        if new_det:
            put(vis, f"k {det.cfg.k_sigma}   (+/- k)   c: calibrate", vis.shape[0] - 40, scale=0.5)
        else:
            put(vis, f"thr {thr}  frames {args.frames}   (+/- thr, [/] frames)", vis.shape[0] - 40, scale=0.5)
        put(vis, "SPACE: green/red   s: save   n: point ids   q: quit", vis.shape[0] - 15, scale=0.5)

        cv2.imshow("RLGL motion test", vis)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord(" ") and state != "CALIB":
            if state == "GREEN":
                state, t_red = "GRACE", now
            else:
                if state in ("RED", "CAUGHT"):
                    n_round += 1
                state, result = "GREEN", ""
                if det is not None:
                    det.stop()
        if key == ord("c") and new_det and state in ("GREEN", "CALIB"):
            state, t_calib = "CALIB", now
            det.start_calibration()
            print(f"CALIBRATE: ยืนนิ่ง {args.calib:.0f} วินาที")
        if key in (ord("+"), ord("=")):           # ปรับ threshold ระหว่างรัน
            if new_det:
                det.cfg.k_sigma = round(det.cfg.k_sigma + 0.25, 2)
                det._qcache.clear()
                print("k =", det.cfg.k_sigma)
            else:
                thr = round(thr + (0.005 if uses_pose else 0.002), 3)
                if det is not None:
                    det.thr = thr
                print("thr =", thr)
        if key in (ord("-"), ord("_")):
            if new_det:
                det.cfg.k_sigma = max(round(det.cfg.k_sigma - 0.25, 2), 1.0)
                det._qcache.clear()
                print("k =", det.cfg.k_sigma)
            else:
                thr = max(round(thr - (0.005 if uses_pose else 0.002), 3), 0.001)
                if det is not None:
                    det.thr = thr
                print("thr =", thr)
        if key == ord("]"):                        # ปรับจำนวนเฟรมติดกันที่ต้องเกิน thr
            args.frames += 1
            if det is not None and not new_det:
                det.frames = args.frames
            print("frames =", args.frames)
        if key == ord("[") and args.frames > 1:
            args.frames -= 1
            if det is not None and not new_det:
                det.frames = args.frames
            print("frames =", args.frames)
        if key == ord("n"):
            show_ids = not show_ids
        if key == ord("s"):
            path = os.path.join("captures", f"{name}_{state}_{int(now)}.jpg")
            cv2.imwrite(path, vis)
            print("saved", path)

    cap.release()
    cv2.destroyAllWindows()
    if rec is not None and rec.save():
        print("saved", rec.path, "-> ประเมินด้วย: python eval_motion.py", rec.path)
    if mp_pose is not None:
        mp_pose.landmarker.close()  # ปิดเองก่อนจบโปรแกรม ไม่งั้นจะมี error ตอน Python ปิดตัว

    # --- สรุปผลไว้ทำ evaluation ---
    elapsed = time.perf_counter() - t_start
    if n_frames < 10:
        return
    warm = infer_list[5:] or infer_list  # ตัดเฟรมแรกๆ ที่ยัง warm-up
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": f"{platform.node()} ({platform.machine()})",
        "method": name,
        "frames": n_frames,
        "avg_fps": round(n_frames / elapsed, 2),
        "infer_ms_mean": round(float(np.mean(warm)), 2),
        "infer_ms_p95": round(float(np.percentile(warm, 95)), 2),
        "cpu_mean_pct": round(float(np.mean(mon.cpu_samples)), 1) if mon.cpu_samples else "",
        "ram_after_load_mb": round(ram_after_load),
        "ram_peak_mb": round(mon.peak_ram),
        "gpu_mb": round(mon.gpu) if mon.gpu is not None else "",
    }
    print("\n=== summary ===")
    for k, v in row.items():
        print(f"{k:>18}: {v}")
    if new_det:
        print(f"{'final k':>18}: {det.cfg.k_sigma}")
    else:
        print(f"{'final thr':>18}: {thr}  (frames {args.frames})")
    save_benchmark(row)


if __name__ == "__main__":
    main()
