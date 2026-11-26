"""
eval_motion.py: วัดความแม่นของตัวตรวจการขยับ แบบเทียบตัวเก่ากับตัวใหม่บนข้อมูลชุดเดียวกัน

แนวคิด: ตอนเล่นจริงให้ pi_detect.py / test_motion.py บันทึก keypoint ทุกเฟรมลงไฟล์ .npz (--record)
แล้วเอาไฟล์นั้นมา "เล่นซ้ำ" ผ่านตัวตรวจทั้งสองแบบ ได้ผลที่เทียบกันได้ตรงๆ และลองปรับเกณฑ์ได้โดยไม่ต้องเล่นใหม่

บันทึกข้อมูล (บน Pi):
    python pi_detect.py --record --label still     # ทุกรอบไฟแดงในไฟล์นี้ ผู้เล่นยืนนิ่ง
    python pi_detect.py --record --label moved     # ทุกรอบไฟแดง ผู้เล่นตั้งใจขยับ (ขยับเล็กบ้างใหญ่บ้าง)
    python pi_detect.py --record --label still,moved   # สลับกันทีละรอบ

ประเมินผล (รันบน laptop ได้ ใช้แค่ numpy):
    python eval_motion.py captures/rec_*.npz                     # เทียบเก่า/ใหม่ + ลองหลายเกณฑ์
    python eval_motion.py --tune captures/tune/*.npz --test captures/test/*.npz
                                                                 # เลือกเกณฑ์จากชุด tune แล้ววัดผลบนชุด test
    python eval_motion.py --demo                                 # ข้อมูลจำลอง (ไว้ลองโค้ด ไม่ใช่ผลจริง)
"""
import argparse
import csv
import glob
import json
import os
import time

import numpy as np

from motion_detector import DetectorConfig, MotionDetector, OldDetector


# ---------------------------------------------------------------- recording
class PoseRecorder:
    """เก็บ keypoint ทุกเฟรม + phase + label ของรอบ แล้วเซฟเป็น .npz"""

    def __init__(self, path, meta=None):
        self.path, self.meta = path, dict(meta or {})
        self.rows = []

    def add(self, t, phase, round_id, label, pose):
        if pose is None:
            xy, conf, h, has = np.zeros((17, 2), np.float32), np.zeros(17, np.float32), 0.0, False
        else:
            xy, conf, h, has = pose[0], pose[1], pose[2], True
        self.rows.append((t, phase, round_id, label or "", has, np.asarray(xy, np.float32),
                          np.asarray(conf, np.float32), float(h)))

    def save(self):
        if not self.rows:
            return None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        t, ph, rid, lab, has, xy, conf, h = zip(*self.rows)
        np.savez_compressed(self.path, t=np.array(t), phase=np.array(ph, dtype="U8"),
                            round=np.array(rid), label=np.array(lab, dtype="U8"), has=np.array(has),
                            xy=np.stack(xy), conf=np.stack(conf), height=np.array(h),
                            meta=np.array(json.dumps(self.meta, ensure_ascii=False)))
        return self.path


def load_rec(path):
    d = np.load(path, allow_pickle=False)
    rec = {k: d[k] for k in d.files}
    rec["meta"] = json.loads(str(rec["meta"]))
    rec["path"] = path
    return rec


# ---------------------------------------------------------------- replay
def replay(rec, make_det):
    """เล่นไฟล์บันทึกซ้ำผ่านตัวตรวจ คืนรายการผลของแต่ละรอบไฟแดง"""
    det = make_det()
    rounds, prev, t0, hit_t, cur_label, cur_round = [], None, 0.0, None, "", -1
    n_valid = n_red = 0

    def close():
        rounds.append({"file": os.path.basename(rec["path"]), "round": int(cur_round), "label": cur_label,
                       "pred": "moved" if det.moved else "still",
                       "detect_s": round(hit_t, 3) if hit_t is not None else None})

    for i in range(len(rec["t"])):
        ph, t = str(rec["phase"][i]), float(rec["t"][i])
        if ph != prev:
            if prev == "CALIB":
                det.finish_calibration()
            if prev == "RED":
                det.stop()
                close()
            if ph == "CALIB":
                det.start_calibration()
            if ph == "RED":
                det.start_red()
                t0, hit_t = t, None
                cur_label, cur_round = str(rec["label"][i]), rec["round"][i]
            prev = ph
        pose = (rec["xy"][i], rec["conf"][i], float(rec["height"][i])) if rec["has"][i] else None
        r = det.update(t, pose)
        if ph == "RED":
            n_red += 1
            n_valid += r.valid
            if r.triggered:
                hit_t = t - t0
    if prev == "RED":
        det.stop()
        close()
    return rounds


def metrics(rounds):
    rs = [r for r in rounds if r["label"] in ("still", "moved")]
    tp = sum(r["label"] == "moved" and r["pred"] == "moved" for r in rs)
    fn = sum(r["label"] == "moved" and r["pred"] == "still" for r in rs)
    fp = sum(r["label"] == "still" and r["pred"] == "moved" for r in rs)
    tn = sum(r["label"] == "still" and r["pred"] == "still" for r in rs)
    n = max(len(rs), 1)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    lat = [r["detect_s"] for r in rs if r["label"] == "moved" and r["detect_s"] is not None]
    return {"n": len(rs), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "accuracy": (tp + tn) / n, "precision": prec, "recall": rec, "f1": f1,
            "fpr": fp / (fp + tn) if fp + tn else 0.0,
            "detect_s_median": float(np.median(lat)) if lat else None}


def run(recs, make_det):
    rounds = []
    for rec in recs:
        rounds += replay(rec, make_det)
    return metrics(rounds), rounds


# ---------------------------------------------------------------- report
OLD_GRID = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08]
NEW_GRID = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 7.0]


def fmt(m):
    lat = f"{m['detect_s_median']:.2f}s" if m["detect_s_median"] is not None else "  -  "
    return (f"acc {m['accuracy']:.3f}  prec {m['precision']:.3f}  recall {m['recall']:.3f}  "
            f"f1 {m['f1']:.3f}  FP {m['fp']:3d} ({m['fpr']:.0%})  FN {m['fn']:3d}  (n={m['n']})  detect {lat}")


def confusion(m, title):
    print(f"\n  {title}")
    print("                 ทำนาย moved   ทำนาย still")
    print(f"    จริง moved   {m['tp']:10d}   {m['fn']:11d}")
    print(f"    จริง still   {m['fp']:10d}   {m['tn']:11d}")


def sweep(recs, max_fpr=0.05, verbose=True):
    """ลองหลายเกณฑ์ของทั้งสองตัว"""
    res = {"old": [], "new": []}
    for thr in OLD_GRID:
        m, _ = run(recs, lambda: OldDetector(thr=thr))
        res["old"].append((thr, m))
    for k in NEW_GRID:
        m, _ = run(recs, lambda: MotionDetector(DetectorConfig(k_sigma=k)))
        res["new"].append((k, m))
    if verbose:
        print("\n--- ตัวเดิม: ปรับ thr (ค่าเดิมในโค้ด 0.05) ---")
        for thr, m in res["old"]:
            print(f"  thr {thr:<6} {fmt(m)}")
        print("\n--- ตัวใหม่: ปรับ k_sigma (ค่าตั้งต้น 4.5) ---")
        for k, m in res["new"]:
            print(f"  k   {k:<6} {fmt(m)}")
    # เลือกค่าที่จับคนขยับได้มากที่สุด โดยจับผิดคนยืนนิ่งไม่เกิน max_fpr (การกล่าวหาคนที่นิ่งแย่กว่าปล่อยหลุด)
    best = {}
    for kind in res:
        ok = [x for x in res[kind] if x[1]["fpr"] <= max_fpr]
        best[kind] = (max(ok, key=lambda x: (x[1]["recall"], -x[1]["fpr"])) if ok
                      else min(res[kind], key=lambda x: (x[1]["fpr"], -x[1]["recall"])))[0]
    if verbose:
        print(f"\n  เลือกเกณฑ์: recall สูงสุดที่ FP rate (คนนิ่งโดนจับผิด) <= {max_fpr:.0%}"
              f" -> เดิม thr={best['old']}, ใหม่ k_sigma={best['new']}")
    return best, res


def write_csv(path, rows, append=False):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not (append and os.path.exists(path))
    with open(path, "a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)
    print("saved", path)


def evaluate(test_recs, old_thr, k, out_dir, title):
    m_old, r_old = run(test_recs, lambda: OldDetector(thr=old_thr))
    m_new, r_new = run(test_recs, lambda: MotionDetector(DetectorConfig(k_sigma=k)))
    print(f"\n===== {title} =====")
    print(f"  เดิม (thr={old_thr}):     {fmt(m_old)}")
    print(f"  ใหม่ (k_sigma={k}):  {fmt(m_new)}")
    confusion(m_old, f"ตัวเดิม thr={old_thr}")
    confusion(m_new, f"ตัวใหม่ k_sigma={k}")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    write_csv(os.path.join(out_dir, "eval_results.csv"), [
        {"time": stamp, "set": title, "detector": "old", "param": f"thr={old_thr}", **_round(m_old)},
        {"time": stamp, "set": title, "detector": "new", "param": f"k_sigma={k}", **_round(m_new)},
    ], append=True)
    rows = [{**a, "pred_old": a["pred"], "detect_old": a["detect_s"], "pred_new": b["pred"],
             "detect_new": b["detect_s"]} for a, b in zip(r_old, r_new)]
    for r in rows:
        del r["pred"], r["detect_s"]
    write_csv(os.path.join(out_dir, "eval_rounds.csv"), rows)
    return m_old, m_new


def _round(m):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}


class _NoCalib(MotionDetector):
    def finish_calibration(self):
        self.mode = "idle"
        return {}


def ablation(recs, k=4.5):
    """ปิดทีละส่วนของตัวใหม่ ดูว่าแต่ละส่วนช่วยเท่าไร (ใช้ใส่สไลด์ได้)"""
    rows = [
        ("ตัวใหม่ครบทุกส่วน", lambda: MotionDetector(DetectorConfig(k_sigma=k))),
        ("- ไม่ calibrate (ใช้ค่าสั่นตั้งต้น)", lambda: _NoCalib(DetectorConfig(k_sigma=k))),
        ("- ไม่รวมหลักฐานเป็นกลุ่ม", lambda: MotionDetector(DetectorConfig(k_sigma=k, group=False))),
        ("- ท่าตั้งต้นจากเฟรมเดียว", lambda: MotionDetector(DetectorConfig(k_sigma=k, ref_frames=1, ref_seconds=0))),
        ("- ไม่กรอง One Euro", lambda: MotionDetector(DetectorConfig(k_sigma=k, euro_min_cutoff=1e4))),
        ("- ต้องเกินติดกัน 3 เฟรม (แบบเดิม)", lambda: MotionDetector(DetectorConfig(k_sigma=k, k_of_n=(3, 3), strong_frames=99))),
    ]
    print(f"\n===== ablation (k_sigma={k}) =====")
    for name, mk in rows:
        m, _ = run(recs, mk)
        print(f"  {name:<36} {fmt(m)}")


# ---------------------------------------------------------------- synthetic demo data
# โครงร่างคนยืนหันหน้าเข้ากล้อง หน่วยเป็นความยาวลำตัว จุดกำเนิดที่กึ่งกลางสะโพก (y ชี้ลง)
BASE = np.array([[0, -1.45], [-0.08, -1.55], [0.08, -1.55], [-0.18, -1.5], [0.18, -1.5],
                 [-0.45, -1.0], [0.45, -1.0], [-0.5, -0.45], [0.5, -0.45], [-0.5, 0.05], [0.5, 0.05],
                 [-0.25, 0], [0.25, 0], [-0.22, 1.0], [0.22, 1.0], [-0.22, 2.0], [0.22, 2.0]], np.float32)
JIT_PX = np.array([0.9] * 5 + [0.7, 0.7, 1.2, 1.2, 1.8, 1.8, 0.9, 0.9, 1.1, 1.1, 1.5, 1.5], np.float32)
HEAD, UPPER = [0, 1, 2, 3, 4], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def _smooth(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def _movement(rng):
    """สุ่มท่าขยับ 1 แบบ คืน (ชื่อ, ฟังก์ชัน offset(เวลาหลังเริ่มขยับ) -> (17,2) หน่วยความยาวลำตัว)"""
    kind = rng.choice(["arm_raise", "hand_twitch", "head_turn", "lean", "step", "slow_drift"])
    side = rng.choice([-1, 1])
    o = np.zeros((17, 2), np.float32)
    if kind == "arm_raise":
        a, dur = rng.uniform(0.10, 0.35), rng.uniform(0.3, 0.6)
        w, e = (9, 7) if side < 0 else (10, 8)
        o[w], o[e] = [side * 0.3 * a, -a], [side * 0.15 * a, -0.4 * a]
        return kind, lambda s: o * _smooth(s / dur)
    if kind == "hand_twitch":
        a, dur = rng.uniform(0.07, 0.15), rng.uniform(0.35, 0.6)
        o[9 if side < 0 else 10] = [side * a * 0.7, -a * 0.7]
        return kind, lambda s: o * (np.sin(np.pi * np.clip(s / dur, 0, 1)))
    if kind == "head_turn":
        a, dur = rng.uniform(0.06, 0.15), rng.uniform(0.3, 0.5)
        o[0], o[[1, 2]], o[[3, 4]] = [side * a, 0], [side * 0.8 * a, 0], [side * 0.5 * a, 0]
        return kind, lambda s: o * _smooth(s / dur)
    if kind == "lean":
        a, dur = rng.uniform(0.06, 0.14), rng.uniform(0.4, 0.8)
        o[UPPER] = [side * a, 0.1 * a]
        o[[11, 12]] = [side * 0.2 * a, 0]
        return kind, lambda s: o * _smooth(s / dur)
    if kind == "step":
        a, dur = rng.uniform(0.15, 0.4), rng.uniform(0.3, 0.5)
        k, an, h = (13, 15, 11) if side < 0 else (14, 16, 12)
        o[an], o[k], o[h] = [side * 0.3 * a, -0.3 * a], [side * 0.2 * a, -0.4 * a], [side * 0.1 * a, 0]
        return kind, lambda s: o * _smooth(s / dur)
    a = rng.uniform(0.06, 0.12)                    # slow_drift: เอนช้าๆ ใน 2 วินาที
    o[UPPER] = [side * a, 0]
    return kind, lambda s: o * _smooth(s / 2.0)


def synth_session(path, rng, labels, n_rounds=10, fps=15.0, green=3.0, grace=0.7, red=3.0, calib=5.0):
    noise = rng.uniform(0.7, 1.6)                  # แสง/ระยะต่างกันในแต่ละ session
    scale = rng.uniform(65, 100)                   # ความยาวลำตัวเป็นพิกเซล
    rec = PoseRecorder(path, {"synthetic": True, "noise": round(noise, 2), "fps": fps})
    jit = np.zeros((17, 2), np.float32)
    common = np.zeros(2, np.float32)
    drop = np.zeros(17, int)
    t = 0.0

    def frame(t, phase, rid, label, offset):
        nonlocal jit, common, drop
        rho, rc = 0.6, 0.8
        jit = rho * jit + np.sqrt(1 - rho ** 2) * rng.normal(0, 1, (17, 2)) * JIT_PX[:, None] * noise
        common = rc * common + np.sqrt(1 - rc ** 2) * rng.normal(0, 0.5, 2)
        center = np.array([320, 250], np.float32)
        xy = center + (BASE + offset) * scale + jit + common
        spikes = rng.random(17) < 0.008
        xy[spikes] += rng.normal(0, 7, (spikes.sum(), 2))
        conf = np.full(17, 0.95, np.float32) - rng.uniform(0, 0.08, 17)
        conf[[9, 10, 15, 16]] -= 0.1
        start = (drop == 0) & (rng.random(17) < np.where(np.isin(np.arange(17), [9, 10, 15, 16]), 0.02, 0.003))
        drop[start] = rng.integers(1, 5, start.sum())
        low = drop > 0
        conf[low] = rng.uniform(0.15, 0.45, low.sum())
        xy[low] += rng.normal(0, 5 * noise, (low.sum(), 2))
        drop = np.maximum(drop - 1, 0)
        ys = xy[conf > 0.5, 1]
        h = float(ys.max() - ys.min()) if len(ys) >= 2 else 1.0
        pose = None if rng.random() < 0.01 else (xy, conf, h)
        rec.add(t, phase, rid, label, pose)

    def step_t(t):
        return t + rng.uniform(0.9, 1.1) / fps

    z = np.zeros((17, 2), np.float32)
    while t < calib:
        frame(t, "CALIB", -1, "", z)
        t = step_t(t)
    for rid in range(n_rounds):
        label = labels[rid % len(labels)]
        if label == "random":
            label = rng.choice(["still", "moved"])
        t_g = t
        stop = green + rng.uniform(0.0, 0.35)       # หยุดเดินช่วงต้นของ grace
        while t < t_g + green + grace + red:
            s = t - t_g
            off = np.zeros((17, 2), np.float32)
            if s < stop:                              # เดิน: ขาแกว่ง แขนแกว่ง ตัวเด้ง
                ph = 2 * np.pi * 1.6 * s
                off[[13, 15]] += [0, -0.15 * max(np.sin(ph), 0)]
                off[[14, 16]] += [0, -0.15 * max(-np.sin(ph), 0)]
                off[[15]] += [0.05 * np.sin(ph), 0]
                off[[7, 9]] += [0, 0.12 * np.sin(ph)]
                off[[8, 10]] += [0, -0.12 * np.sin(ph)]
                off[:, 1] += 0.02 * np.sin(2 * ph)
            else:                                     # เพิ่งหยุด: ตัวโยกเล็กน้อยแล้วค่อยๆ นิ่ง
                off[UPPER, 0] += 0.03 * np.exp(-(s - stop) / 0.25) * np.sin(2 * np.pi * 2 * (s - stop))
            off[UPPER, 1] += 0.004 * np.sin(2 * np.pi * 0.25 * t)   # หายใจ
            if s < green:
                phase = "GREEN"
            elif s < green + grace:
                phase = "GRACE"
            else:
                phase = "RED"
                if label == "moved":
                    if not hasattr(frame, "mv") or frame.mv_round != rid:
                        frame.mv_round, frame.mv = rid, _movement(rng)
                        frame.onset = rng.uniform(0.3, 2.0)
                        rec.meta.setdefault("moves", {})[str(rid)] = [frame.mv[0], round(frame.onset, 2)]
                    sr = s - green - grace - frame.onset
                    if sr > 0:
                        off += frame.mv[1](sr)
            frame(t, phase, rid, label, off)
            t = step_t(t)
    rec.save()
    return path


def demo(out_dir):
    print("=" * 78)
    print(" DEMO ข้อมูลจำลอง: keypoint สังเคราะห์ (jitter, จุดหาย, ขยับแบบต่างๆ) ไม่ใช่ข้อมูลจากกล้องจริง")
    print(" ใช้ตรวจว่าโค้ดทำงานถูกและดูกลไกที่ต่างกัน ตัวเลข accuracy จริงต้องวัดจากไฟล์ที่ --record บน Pi")
    print("=" * 78)
    rng = np.random.default_rng(7)
    d = os.path.join(out_dir, "synthetic")
    tune = [load_rec(synth_session(os.path.join(d, f"tune_{i}.npz"), rng, ["still", "moved"])) for i in range(10)]
    test = [load_rec(synth_session(os.path.join(d, f"test_{i}.npz"), rng, ["random"])) for i in range(20)]
    return tune, test


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="ไฟล์ .npz ที่บันทึกไว้")
    ap.add_argument("--tune", nargs="*", default=None, help="ไฟล์สำหรับเลือกเกณฑ์")
    ap.add_argument("--test", nargs="*", default=None, help="ไฟล์สำหรับวัดผล (ห้ามซ้ำกับ --tune)")
    ap.add_argument("--old-thr", type=float, default=None, help="บังคับใช้ thr ของตัวเดิม")
    ap.add_argument("--k", type=float, default=None, help="บังคับใช้ k_sigma ของตัวใหม่")
    ap.add_argument("--demo", action="store_true", help="สร้างข้อมูลจำลองแล้วประเมิน")
    ap.add_argument("--ablation", action="store_true", help="ปิดทีละส่วนของตัวใหม่ ดูว่าแต่ละส่วนช่วยเท่าไร")
    ap.add_argument("--max-fpr", type=float, default=0.05, help="FP rate สูงสุดที่ยอมรับตอนเลือกเกณฑ์")
    ap.add_argument("--out", default="captures")
    args = ap.parse_args()

    def expand(lst):
        out = []
        for p in lst:
            out += sorted(glob.glob(p)) or [p]
        return [load_rec(p) for p in out]

    if args.demo:
        tune, test = demo(args.out)
    elif args.tune is not None or args.test is not None:
        tune, test = expand(args.tune or []), expand(args.test or [])
    else:
        tune = test = expand(args.files)
    if not test:
        raise SystemExit("ไม่มีไฟล์ให้ประเมิน")

    # 1) ค่าเดิมในโค้ด
    evaluate(test, 0.05, 4.5, args.out, "ค่าตั้งต้นของแต่ละตัว (เดิม thr 0.05 / ใหม่ k_sigma 4.5)")

    # 2) ให้ทั้งสองตัวได้ปรับเกณฑ์ที่ดีที่สุดก่อนเทียบ (เทียบแบบยุติธรรม)
    if tune:
        same = tune is test
        print(f"\n######## ลองหลายเกณฑ์บนชุด {'เดียวกับที่วัดผล (ผลจะดีเกินจริง)' if same else 'tune'} ########")
        best, _ = sweep(tune, args.max_fpr)
        old_thr = args.old_thr if args.old_thr is not None else best["old"]
        k = args.k if args.k is not None else best["new"]
        evaluate(test, old_thr, k, args.out, f"เกณฑ์ที่ดีที่สุดของแต่ละตัว วัดบนชุด {'เดียวกัน' if same else 'test'}")
    if args.ablation:
        ablation(test, args.k if args.k is not None else 4.5)


if __name__ == "__main__":
    main()
