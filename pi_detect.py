"""
ทดสอบ MediaPipe Pose บน Raspberry Pi: ใช้แค่กล้อง ยังไม่ต่อ MQTT / sensor อื่น
จำลองเกมเองอัตโนมัติ: ไฟเขียว -> grace -> ไฟแดง วนไปเรื่อยๆ แล้วพิมพ์ผลว่าขยับหรือไม่

ต้องวางไฟล์นี้ไว้โฟลเดอร์เดียวกับ test_motion.py (ใช้ฟังก์ชันคำนวณการขยับชุดเดียวกัน)

ติดตั้งบน Raspberry Pi OS 64-bit:
    sudo apt update && sudo apt install -y python3-picamera2      # ใช้กับกล้อง CSI (Camera Module)
    python3 -m venv --system-site-packages ~/rlgl-venv             # --system-site-packages เพื่อให้เห็น picamera2
    source ~/rlgl-venv/bin/activate
    pip install mediapipe psutil                                    # mediapipe ลง OpenCV มาให้แล้ว

รัน:
    python pi_detect.py                          # กล้อง CSI, พิมพ์ผลใน terminal (ใช้ผ่าน SSH ได้)
    python pi_detect.py --source 0               # กล้อง USB
    python pi_detect.py --show                   # เปิดหน้าต่างภาพ (ต้องมีจอหรือ VNC)
    python pi_detect.py --green 3 --red 5        # ปรับเวลาไฟเขียว/แดง (วินาที)
    python pi_detect.py --duration 120           # รัน 2 นาทีแล้วสรุปผลเอง
    python pi_detect.py --detector old           # ใช้ตัวตรวจแบบเดิม (ไว้เทียบ)
    python pi_detect.py --record --label still   # บันทึก keypoint ไว้ประเมินด้วย eval_motion.py
                                                 # (--label moved = ทุกรอบตั้งใจขยับ, still,moved = สลับทีละรอบ)
เริ่มต้นจะมีช่วง CALIBRATE ให้ยืนนิ่ง (--calib วินาที, 0 = ข้าม) เพื่อวัดว่าแต่ละจุดสั่นแค่ไหนตอนนิ่ง
หยุดด้วย Ctrl+C (หรือกด q ถ้าใช้ --show) แล้วจะพิมพ์สรุปและบันทึกลง captures/benchmark.csv
"""
import argparse
import os
import platform
import time

import cv2
import numpy as np

from eval_motion import PoseRecorder
from motion_detector import DetectorConfig, MotionDetector, OldDetector
from test_motion import MediaPipePose, ResourceMonitor, draw_mp_all, put, save_benchmark


class Camera:
    """อ่านภาพจากกล้อง CSI (Picamera2) หรือกล้อง USB (OpenCV) ให้ได้ภาพ BGR เหมือนกัน"""

    def __init__(self, source, width, height):
        self.picam, self.cap = None, None
        if source == "picam":
            from picamera2 import Picamera2
            self.picam = Picamera2()
            # "RGB888" ของ picamera2 เรียงไบต์เป็น BGR ใช้กับ OpenCV ได้ตรงๆ
            cfg = self.picam.create_video_configuration(main={"size": (width, height), "format": "RGB888"})
            self.picam.configure(cfg)
            self.picam.start()
            time.sleep(1.0)  # รอให้กล้องปรับแสง
        else:
            self.cap = cv2.VideoCapture(int(source))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if not self.cap.isOpened():
                raise SystemExit(f"เปิดกล้อง {source} ไม่ได้")

    def read(self):
        if self.picam is not None:
            return self.picam.capture_array()
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        if self.picam is not None:
            self.picam.stop()
        if self.cap is not None:
            self.cap.release()


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def phase_at(t, green, grace, red):
    x = t % (green + grace + red)
    if x < green:
        return "GREEN"
    if x < green + grace:
        return "GRACE"
    return "RED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="picam", help='"picam" = กล้อง CSI, หรือเลข index ของกล้อง USB')
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--variant", choices=["lite", "full", "heavy"], default="lite")
    ap.add_argument("--detector", choices=["new", "old"], default="new")
    ap.add_argument("--k", type=float, default=4.5, help="ตัวใหม่: ความไว ยิ่งต่ำยิ่งไว (เกณฑ์ = k x ค่าสั่นของแต่ละจุด)")
    ap.add_argument("--thr", type=float, default=0.05, help="ตัวเดิม: เกณฑ์ (สัดส่วนของความสูงตัว)")
    ap.add_argument("--frames", type=int, default=3, help="ตัวเดิม: ต้องเกิน thr ติดกันกี่เฟรมถึงนับว่าขยับ")
    ap.add_argument("--calib", type=float, default=5.0, help="วินาทีที่ให้ยืนนิ่งก่อนเริ่ม (0 = ข้าม)")
    ap.add_argument("--record", action="store_true", help="บันทึก keypoint ทุกเฟรมลง captures/rec_*.npz")
    ap.add_argument("--label", default="", help="ป้ายของรอบไฟแดงที่บันทึก: still / moved / still,moved (สลับทีละรอบ)")
    ap.add_argument("--green", type=float, default=4.0)
    ap.add_argument("--grace", type=float, default=0.7)
    ap.add_argument("--red", type=float, default=4.0)
    ap.add_argument("--duration", type=float, default=0, help="วินาที (0 = รันจนกด Ctrl+C)")
    ap.add_argument("--show", action="store_true", help="เปิดหน้าต่างภาพ (ต้องมีจอ)")
    args = ap.parse_args()

    if args.show and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("ไม่พบจอ (DISPLAY) ปิด --show ให้อัตโนมัติ")
        args.show = False

    os.makedirs("captures", exist_ok=True)
    det = (OldDetector(thr=args.thr, frames=args.frames) if args.detector == "old"
           else MotionDetector(DetectorConfig(k_sigma=args.k)))
    labels = [x.strip() for x in args.label.split(",") if x.strip()] or [""]
    rec = None
    if args.record:
        rec = PoseRecorder(os.path.join("captures", f"rec_{time.strftime('%Y%m%d_%H%M%S')}_{args.label.replace(',', '-') or 'nolabel'}.npz"),
                           {"source": args.source, "size": [args.width, args.height], "variant": args.variant,
                            "green": args.green, "grace": args.grace, "red": args.red, "labels": labels})
    pose = MediaPipePose(args.variant)
    cam = Camera(args.source, args.width, args.height)
    mon = ResourceMonitor()
    ram_after_load = mon.ram

    sens = f"k={args.k}" if args.detector == "new" else f"thr={args.thr} frames={args.frames}"
    print(f"start: mediapipe-{args.variant} {args.width}x{args.height} detector={args.detector} {sens} "
          f"cycle green {args.green}s / grace {args.grace}s / red {args.red}s")
    if args.calib > 0:
        print(f"CALIBRATE: ยืนนิ่งในกล้อง {args.calib:.0f} วินาที")

    t_start = time.perf_counter()
    t_prev, t_log = t_start, t_start
    fps, infer_ms, infer_list, n_frames = 0.0, 0.0, [], 0
    prev_phase, caught, r = None, False, None
    rounds, rounds_caught, score, part = 0, 0, None, None

    try:
        while True:
            frame = cam.read()
            if frame is None:
                print("อ่านภาพไม่ได้")
                break

            t_inf = time.perf_counter()
            cur = pose(frame)
            dt = time.perf_counter() - t_inf
            infer_list.append(dt * 1000)
            infer_ms = 0.9 * infer_ms + 0.1 * dt * 1000 if infer_ms else dt * 1000

            now = time.perf_counter()
            el = now - t_start
            phase = "CALIB" if el < args.calib else phase_at(el - args.calib, args.green, args.grace, args.red)

            # --- เปลี่ยน phase ---
            if phase != prev_phase:
                if prev_phase == "CALIB":
                    info = det.finish_calibration()
                    if info:
                        print(f"  calibrate: {info.get('frames')} เฟรม, noise x{info.get('noise_factor')}"
                              + (f"  ** {info['msg']}" if info.get("msg") else ""))
                if prev_phase == "RED":
                    det.stop()
                    rounds += 1
                    rounds_caught += caught
                    print(f"  round {rounds}: {'MOVED' if caught else 'STILL'}"
                          + (f"  (label {labels[(rounds - 1) % len(labels)]})" if labels[0] else ""))
                if phase == "CALIB":
                    det.start_calibration()
                if phase == "RED":
                    det.start_red()       # ตัวใหม่เก็บท่าตั้งต้นจากเฟรมแรกๆ ของไฟแดง (~0.3 วิ) ก่อนเริ่มตัดสิน
                    caught = False
                prev_phase = phase

            r = det.update(now, cur)
            if rec is not None:
                rec.add(now, phase, rounds, labels[rounds % len(labels)] if phase == "RED" else "", cur)

            # --- ตัดสินตอนไฟแดง ---
            score, part = r.score, r.part
            if r.triggered:
                caught = True
                print(f"  >>> MOVED: {r.part}/{r.joint} score={r.score:.2f}")
                img = frame.copy()
                draw_mp_all(img, pose.all_points)
                put(img, f"MOVED: {r.part}/{r.joint} {r.score:.2f}", 28, (0, 0, 255))
                cv2.imwrite(os.path.join("captures", f"pi_moved_{int(time.time())}.jpg"), img)

            # --- FPS + resource ---
            inst = 1.0 / max(now - t_prev, 1e-6)
            fps = 0.9 * fps + 0.1 * inst if fps else inst
            t_prev = now
            n_frames += 1
            mon.update()

            if now - t_log >= 1.0:
                t_log = now
                temp = cpu_temp()
                s = f"[{phase:5}] FPS {fps:4.1f} | infer {infer_ms:5.1f} ms | {mon.text()}"
                if temp is not None:
                    s += f" | {temp:.0f}°C"
                if phase == "RED":
                    if r is not None and r.mode == "ref":
                        s += " | building reference"
                    else:
                        s += " | no player" if score is None else f" | score {score:.2f} ({part})"
                print(s)

            if args.show:
                vis = frame.copy()
                draw_mp_all(vis, pose.all_points)
                put(vis, f"{phase}  FPS {fps:4.1f}  infer {infer_ms:5.1f} ms", 28)
                if score is not None:
                    put(vis, f"score {score:.2f} / 1.00 [{part}]", 54, (0, 0, 255) if score > 1 else (255, 255, 255))
                cv2.imshow("pi_detect", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            if args.duration and now - t_start >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
        pose.landmarker.close()
        if rec is not None and rec.save():
            print("saved", rec.path, "-> ประเมินด้วย: python eval_motion.py", rec.path)
        if args.show:
            cv2.destroyAllWindows()

    elapsed = time.perf_counter() - t_start
    if n_frames < 10:
        return
    warm = infer_list[5:] or infer_list
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": f"{platform.node()} ({platform.machine()})",
        "method": f"mediapipe-{args.variant}@{args.width}x{args.height}+{args.detector}",
        "frames": n_frames,
        "avg_fps": round(n_frames / elapsed, 2),
        "infer_ms_mean": round(float(np.mean(warm)), 2),
        "infer_ms_p95": round(float(np.percentile(warm, 95)), 2),
        "cpu_mean_pct": round(float(np.mean(mon.cpu_samples)), 1) if mon.cpu_samples else "",
        "ram_after_load_mb": round(ram_after_load),
        "ram_peak_mb": round(mon.peak_ram),
        "gpu_mb": "",
    }
    print("\n=== summary ===")
    for k, v in row.items():
        print(f"{k:>18}: {v}")
    print(f"{'red rounds':>18}: {rounds} (moved {rounds_caught}, still {rounds - rounds_caught})")
    save_benchmark(row)


if __name__ == "__main__":
    main()
