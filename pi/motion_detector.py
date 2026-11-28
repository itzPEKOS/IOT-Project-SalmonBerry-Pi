"""
motion_detector.py: ตัดสินว่าผู้เล่น "ขยับ" หรือไม่ช่วงไฟแดง จาก keypoint 17 จุด (COCO)
ใช้ได้ทั้ง MediaPipe Pose (ผ่าน MP_TO_COCO ใน test_motion.py) และ YOLO-pose

ไฟล์นี้ใช้แค่ numpy ไม่ต้องมี OpenCV / MediaPipe จึง import ใน game engine หรือ eval_motion.py ได้เลย

มี 2 ตัว ใช้ API เดียวกัน สลับกันได้:
    MotionDetector  ตัวใหม่ (ค่าเริ่มต้น)
    OldDetector     ตรรกะเดิมใน test_motion.py / pi_detect.py ทุกบรรทัด เก็บไว้เทียบผล

ลำดับการเรียก (ตรงกับ state ของเกม):
    det = MotionDetector()
    det.start_calibration()          # CALIBRATE: ผู้เล่นยืนนิ่ง (เช่น 3-5 วินาที)
    det.update(t, pose)  ทุกเฟรม     # เรียกทุกเฟรม ทุก state (ให้ตัวกรองอุ่นเครื่องไว้ก่อนไฟแดง)
    det.finish_calibration()
    det.start_red()                  # เริ่ม RED_LIGHT (หลังหมดช่วงผ่อนผัน)
    r = det.update(t, pose)          # r.moved == True คือจับได้ว่าขยับ
    det.stop()                       # ออกจาก RED_LIGHT

pose = (xy[17,2] พิกเซล, conf[17] 0-1, height) หรือ None ถ้าไม่เจอคน (รูปแบบเดียวกับ MediaPipePose เดิม)
t = วินาที (เช่น time.perf_counter())

ตัวใหม่ต่างจากเดิมตรงไหน
    1. calibrate: วัดว่าแต่ละจุดสั่นแค่ไหนตอนยืนนิ่ง (ต่อคน ต่อแสง ต่อระยะ) แล้วตั้งเกณฑ์ของแต่ละจุดเอง
       เดิมใช้เกณฑ์ 0.05 เดียวทุกจุดทุกสถานการณ์
    2. วัดทีละจุดแล้วเอาค่ามากสุด: เดิมเฉลี่ยในส่วนของร่างกาย (ไหล่+ศอก+ข้อมือ) ข้อมือขยับคนเดียวจึงถูกหาร 3
    3. รวมหลักฐานเป็นกลุ่ม: หลายจุดเลื่อนไปทางเดียวกัน (เอนตัว หันหัว) จับได้แม้ทีละจุดยังไม่ถึงเกณฑ์
    4. หารด้วยความยาวลำตัว (ไหล่-สะโพก) แทนความสูงจากจุดที่มองเห็น ซึ่งเปลี่ยนเมื่อขาหลุดเฟรม
    5. ท่าตั้งต้นใช้ median ของหลายเฟรม (~0.3 วิแรกของไฟแดง) แทนเฟรมเดียว
       ช่วงนี้ยังไม่ตัดสิน ถ้าอยากให้เวลาผ่อนผันรวมเท่าเดิม ลด grace ลง 0.3 วิ
    6. กรอง jitter ด้วย One Euro filter และ hysteresis ของ visibility (จุดไม่กะพริบเข้าออก)
    7. ต้องเกินเกณฑ์ 3 ใน 4 เฟรม (เฟรมเดียวหลุดไม่ทำให้นับใหม่) หรือเกิน 2.5 เท่าติดกัน 2 เฟรม (ขยับแรงจับเร็ว)
    8. score ทุกตัวเป็นค่าเทียบเกณฑ์ (1.0 = เกณฑ์) ใช้เส้นเกณฑ์เส้นเดียวบน dashboard ได้
"""
from collections import deque
from dataclasses import dataclass, asdict

import numpy as np

JOINTS = ["nose", "l_eye", "r_eye", "l_ear", "r_ear", "l_shoulder", "r_shoulder", "l_elbow", "r_elbow",
          "l_wrist", "r_wrist", "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle"]
JOINT_PART = ["head"] * 5 + ["torso", "torso", "left_arm", "right_arm", "left_arm", "right_arm",
                             "torso", "torso", "left_leg", "right_leg", "left_leg", "right_leg"]
L_SH, R_SH, L_HIP, R_HIP = 5, 6, 11, 12

# ค่าสั่นของระยะเลื่อนแต่ละจุดตอนยืนนิ่ง ต่อแกน (หน่วย = ความยาวลำตัว) ใช้เมื่อยังไม่ได้ calibrate
# ปลายแขนปลายขาสั่นมากกว่าไหล่/สะโพก จึงต้องใช้เกณฑ์ต่างกัน ค่านี้เป็นค่าตั้งต้นคร่าวๆ ให้ calibrate ทับ
DEFAULT_SIGMA = np.array([0.012, 0.012, 0.012, 0.014, 0.014,   # หน้า
                          0.010, 0.010, 0.016, 0.016,          # ไหล่ ศอก
                          0.022, 0.022,                        # ข้อมือ
                          0.012, 0.012, 0.016, 0.016,          # สะโพก เข่า
                          0.020, 0.020], dtype=np.float32)     # ข้อเท้า

# กลุ่มจุดที่มักขยับไปด้วยกัน: ถ้าหลายจุดในกลุ่มเลื่อนไปพร้อมกัน แม้ทีละจุดจะยังไม่ถึงเกณฑ์ ก็นับว่าขยับ
# (noise ของแต่ละจุดเป็นอิสระต่อกัน แต่การขยับจริงทำให้หลายจุดเลื่อนพร้อมกัน)
GROUPS = {
    "head": [0, 1, 2, 3, 4],
    "left_arm": [7, 9],
    "right_arm": [8, 10],
    "torso": [5, 6, 11, 12],
    "left_leg": [13, 15],
    "right_leg": [14, 16],
    "upper_body": list(range(11)),   # เอนตัว / โยกตัว
}


def _norm_ppf(p):
    """quantile ของ normal (Acklam) ใช้แทน scipy"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549671324989504e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    q = min(max(min(p, 1 - p), 1e-300), 0.5)
    r = np.sqrt(-2 * np.log(q))
    x = (((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) / \
        ((((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1)
    return x if p < 0.5 else -x   # x เป็นค่าลบสำหรับหางล่าง


def chi2_isf(p, dof):
    """ค่า x ที่ P(chi2(dof) > x) = p (Wilson-Hilferty)"""
    z = -_norm_ppf(p)
    h = 2.0 / (9 * dof)
    return dof * (1 - h + z * np.sqrt(h)) ** 3


@dataclass
class DetectorConfig:
    k_sigma: float = 4.5          # เกณฑ์ของแต่ละจุด = k_sigma x ค่าสั่นของจุดนั้น (ยิ่งสูงยิ่งไม่ไว)
    group: bool = True            # ใช้การรวมหลักฐานจากหลายจุดในกลุ่มเดียวกันด้วย
    min_disp: float = 0.02        # เกณฑ์ต่ำสุด (ความยาวลำตัว) กันเกณฑ์ต่ำเกินถ้าตอน calibrate นิ่งมาก
    ref_frames: int = 4           # จำนวนเฟรมขั้นต่ำที่ใช้สร้างท่าตั้งต้น
    ref_seconds: float = 0.3      # และต้องเก็บนานอย่างน้อยกี่วินาที
    k_of_n: tuple = (3, 4)        # ต้องเกินเกณฑ์ 3 ใน 4 เฟรมล่าสุด (เดิมต้องติดกัน 3 เฟรม)
    strong_ratio: float = 2.5     # ถ้าเกินเกณฑ์ 2.5 เท่า ...
    strong_frames: int = 2        # ... ติดกัน 2 เฟรม ให้จับเลย (ขยับแรง จับได้เร็วขึ้น)
    vis_on: float = 0.6           # จุดจะเริ่มถูกใช้เมื่อ visibility > 0.6
    vis_off: float = 0.4          # และเลิกใช้เมื่อ < 0.4 (hysteresis กันจุดกะพริบเข้าออก)
    min_joints: int = 4           # ต้องเห็นอย่างน้อยกี่จุดถึงจะตัดสิน
    euro_min_cutoff: float = 4.0  # One Euro filter: ยิ่งต่ำยิ่งเรียบตอนนิ่ง (ต่ำไปจะหน่วงและกลบการขยับเร็ว)
    euro_beta: float = 3.0        # ยิ่งสูงยิ่งตามทันตอนขยับเร็ว
    euro_d_cutoff: float = 1.0
    sigma_cap: float = 3.0        # calibrate ได้ค่าสั่นเกินค่าตั้งต้นกี่เท่าถือว่าผู้เล่นขยับตอน calibrate


@dataclass
class MotionResult:
    mode: str = "idle"            # idle / calib / ref / judge
    valid: bool = False           # เห็นคนพอจะตัดสินไหม (False นานเกิน 2 วิ ให้ game ยกเลิกรอบ)
    moved: bool = False           # จับได้แล้วในรอบไฟแดงนี้ (ค้างไว้จนกว่าจะ start_red ใหม่)
    triggered: bool = False       # เพิ่งจับได้ในเฟรมนี้
    score: float = None           # ค่าการขยับเทียบเกณฑ์ (1.0 = เท่าเกณฑ์) ใช้ทำ motion score บน dashboard
    raw: float = None             # ระยะเลื่อนจริงของจุดนั้น (หน่วยความยาวลำตัว)
    joint: str = None             # จุดที่ขยับมากที่สุด
    part: str = None              # ส่วนของร่างกาย
    n_joints: int = 0             # จำนวนจุดที่ใช้ตัดสินในเฟรมนี้
    confidence: float = 0.0       # visibility เฉลี่ยของจุดที่ใช้

    def to_dict(self):
        d = asdict(self)
        for k in ("score", "raw"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        d["confidence"] = round(d["confidence"], 3)
        return d


# ---------------------------------------------------------------- helpers
def torso_length(xy, ok):
    """ความยาวลำตัว = ระยะจากกึ่งกลางไหล่ถึงกึ่งกลางสะโพก (ไม่ขึ้นกับว่าเห็นขาหรือเปล่า)
    ถ้าไม่เห็นสะโพก ใช้ความกว้างไหล่ x 1.6 แทน"""
    if ok[L_SH] and ok[R_SH] and ok[L_HIP] and ok[R_HIP]:
        return float(np.linalg.norm((xy[L_SH] + xy[R_SH]) / 2 - (xy[L_HIP] + xy[R_HIP]) / 2))
    if ok[L_SH] and ok[R_SH]:
        return float(np.linalg.norm(xy[L_SH] - xy[R_SH]) * 1.6)
    return None


class OneEuro:
    """One Euro filter ต่อจุด (Casiez et al. 2012): ตอนนิ่งกรองแรงเพื่อลดการสั่น ตอนขยับเร็วกรองเบาเพื่อไม่ให้ช้า"""

    def __init__(self, n, min_cutoff, beta, d_cutoff):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = np.zeros((n, 2), np.float32)
        self.dx = np.zeros((n, 2), np.float32)
        self.init = np.zeros(n, bool)
        self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * np.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t, xy, mask, scale):
        dt = 1 / 15 if self.t is None else max(t - self.t, 1e-3)
        self.t = t
        new = mask & ~self.init                  # จุดที่เพิ่งเห็น: เริ่มจากค่าดิบ
        self.x[new], self.dx[new], self.init[new] = xy[new], 0, True
        upd = mask & ~new
        if upd.any():
            d = (xy[upd] - self.x[upd]) / dt
            a_d = self._alpha(self.d_cutoff, dt)
            self.dx[upd] = a_d * d + (1 - a_d) * self.dx[upd]
            speed = np.linalg.norm(self.dx[upd], axis=1) / max(scale, 1.0)   # ความยาวลำตัว/วินาที
            cutoff = self.min_cutoff + self.beta * speed
            a = self._alpha(cutoff, dt)[:, None]
            self.x[upd] = a * xy[upd] + (1 - a) * self.x[upd]
        self.init[~mask] = False                 # จุดที่หายไป: เริ่มใหม่เมื่อกลับมา
        return self.x.copy()


# ---------------------------------------------------------------- new detector
class MotionDetector:
    name = "new"

    def __init__(self, cfg=None, **kw):
        self.cfg = cfg or DetectorConfig(**kw)
        c = self.cfg
        self.sigma = DEFAULT_SIGMA.copy()
        self.calibrated = False
        self.calib_info = {}
        self.euro = OneEuro(17, c.euro_min_cutoff, c.euro_beta, c.euro_d_cutoff)
        self.active = np.zeros(17, bool)
        self.scale_est = None
        self.mode = "idle"
        self._qcache = {}
        self._reset_round()

    def _reset_round(self):
        self.ref_buf, self.ref, self.ref_ok, self.ref_scale, self.t_ref0 = [], None, None, None, None
        self.window = deque(maxlen=self.cfg.k_of_n[1])
        self.last_parts = {}
        self.strong_streak, self.moved = 0, False
        self.calib_buf = []

    # ---- state control
    def start_calibration(self):
        self._reset_round()
        self.mode = "calib"

    def finish_calibration(self):
        """ประมาณค่าสั่นของ "ระยะเลื่อนจากท่าตั้งต้น" ของแต่ละจุด จากช่วงยืนนิ่ง
        ทำแบบเดียวกับตอนไฟแดงจริง: สร้างท่าตั้งต้นจากหลายจุดเริ่ม แล้ววัดว่าเฟรมหลังจากนั้นเลื่อนไปเท่าไร
        จึงรวมผลของ noise ในท่าตั้งต้น และการสั่นที่ต่อเนื่องหลายเฟรมไว้แล้ว ใช้ MAD (ทนต่อเฟรมที่เพี้ยน)"""
        self.mode = "idle"
        buf = self.calib_buf
        nref = self.cfg.ref_frames
        info = {"frames": len(buf), "ok": False}
        if len(buf) < nref + 10:
            info["msg"] = "เฟรมน้อยเกินไป ใช้ค่าตั้งต้น"
            self.calib_info = info
            return info
        P = np.stack([b[0] for b in buf])                     # (F,17,2) ตำแหน่งหลังกรอง
        M = np.stack([b[1] for b in buf])                     # (F,17) จุดที่ใช้ได้
        scale = float(np.median([b[2] for b in buf]))
        res = [[] for _ in range(17)]
        for s0 in range(0, len(buf) - nref - 3, 2):
            ref = np.median(P[s0:s0 + nref], axis=0)
            okr = M[s0:s0 + nref].all(axis=0)
            d = (P[s0 + nref:] - ref) / scale                 # (f,17,2)
            m = M[s0 + nref:] & okr
            for j in range(17):
                if m[:, j].any():
                    res[j].append(d[m[:, j], j].ravel())
        est = np.full(17, np.nan)
        for j in range(17):
            r = np.concatenate(res[j]) if res[j] else np.zeros(0)
            if len(r) >= 20:
                est[j] = 1.4826 * np.median(np.abs(r - np.median(r)))
        if np.isnan(est).all():
            info["msg"] = "ไม่เห็นจุดไหนนานพอ ใช้ค่าตั้งต้น"
            self.calib_info = info
            return info
        # ค่าที่วัดทีละจุดจาก 3-5 วินาทีแกว่งมาก (เฟรมติดกันสั่นไปทางเดียวกัน) จึงหา "ระดับ noise รวม"
        # ของทั้งตัวก่อน แล้วใช้ค่าที่มากกว่าระหว่าง (ค่าที่วัดได้ของจุดนั้น) กับ (ระดับรวม x สัดส่วนตั้งต้นของจุดนั้น)
        factor = float(np.nanmedian(est / DEFAULT_SIGMA))
        factor = min(max(factor, 0.3), self.cfg.sigma_cap)
        pooled = factor * DEFAULT_SIGMA
        sig = np.where(np.isnan(est), pooled, np.maximum(est, pooled))
        cap = DEFAULT_SIGMA * self.cfg.sigma_cap * 1.5
        n_capped = int((sig > cap).sum())
        sig = np.minimum(sig, cap)
        self.sigma, self.calibrated = sig.astype(np.float32), True
        info.update(ok=n_capped <= 3 and factor < self.cfg.sigma_cap, capped=n_capped,
                    noise_factor=round(factor, 2), scale_px=round(scale, 1),
                    sigma={JOINTS[j]: round(float(sig[j]), 4) for j in range(17)})
        if not info["ok"]:
            info["msg"] = "noise สูงผิดปกติ ผู้เล่นน่าจะขยับตอน calibrate ควรทำใหม่"
        self.calib_info = info
        return info

    def start_red(self):
        self._reset_round()
        self.mode = "ref"

    def stop(self):
        self.mode = "idle"

    def thresholds(self):
        return np.maximum(self.cfg.min_disp, self.cfg.k_sigma * self.sigma)

    def _q_thr(self, n):
        """เกณฑ์ของกลุ่ม n จุด ให้โอกาสจับผิดตอนนิ่งเท่ากับเกณฑ์ทีละจุด (k_sigma บน Rayleigh)"""
        if n not in self._qcache:
            p = float(np.exp(-self.cfg.k_sigma ** 2 / 2))
            self._qcache[n] = chi2_isf(p, 2 * n)
        return self._qcache[n]

    # ---- per frame
    def update(self, t, pose):
        c = self.cfg
        res = MotionResult(mode=self.mode, moved=self.moved)
        if pose is None:
            self.active[:] = False
            self.euro.init[:] = False
            return res
        xy, conf = np.asarray(pose[0], np.float32), np.asarray(pose[1], np.float32)
        self.active = np.where(self.active, conf > c.vis_off, conf > c.vis_on)
        s = torso_length(xy, self.active)
        if s is not None:
            self.scale_est = s if self.scale_est is None else 0.8 * self.scale_est + 0.2 * s
        pos = self.euro(t, xy, self.active, self.scale_est or 100.0)

        if self.mode == "calib":
            if self.scale_est:
                self.calib_buf.append((pos, self.active.copy(), self.scale_est))
            return res

        if self.mode == "ref":
            self.ref_buf.append((pos, self.active.copy(), conf.copy(), s))
            if self.t_ref0 is None:
                self.t_ref0 = t
            if len(self.ref_buf) >= c.ref_frames and t - self.t_ref0 >= c.ref_seconds:
                self._build_ref()
            return res

        if self.mode != "judge":
            return res

        ok = self.ref_ok & self.active
        res.n_joints = int(ok.sum())
        if res.n_joints < c.min_joints:
            return res
        res.valid = True
        res.confidence = float(conf[ok].mean())
        d = (pos - self.ref) / self.ref_scale               # (17,2) หน่วยความยาวลำตัว
        disp = np.linalg.norm(d, axis=1)
        # 1) ทีละจุด: ระยะเลื่อนเทียบเกณฑ์ของจุดนั้น
        ratio = np.where(ok, disp / self.thresholds(), 0.0)
        self.last_parts = {}
        for jj in np.flatnonzero(ok):
            p = JOINT_PART[jj]
            self.last_parts[p] = max(self.last_parts.get(p, 0.0), float(ratio[jj]))
        j = int(ratio.argmax())
        res.score, res.raw, res.joint, res.part = float(ratio[j]), float(disp[j]), JOINTS[j], JOINT_PART[j]
        # 2) ทั้งกลุ่ม: รวม (ระยะเลื่อน/ค่าสั่น)^2 ของทุกจุดในกลุ่ม เทียบกับ chi-square ที่ระดับเดียวกัน
        if c.group:
            z2 = np.where(ok, (disp / self.sigma) ** 2, 0.0)
            for g, idx in GROUPS.items():
                n = int(ok[idx].sum())
                if n < 2:
                    continue
                gr = float(np.sqrt(z2[idx].sum() / self._q_thr(n)))
                self.last_parts[g] = max(self.last_parts.get(g, 0.0), gr)
                if gr > res.score:
                    jj = idx[int(np.argmax(z2[idx]))]
                    res.score, res.raw, res.joint, res.part = gr, float(disp[jj]), JOINTS[jj], g

        above = res.score > 1.0
        self.window.append(above)
        self.strong_streak = self.strong_streak + 1 if res.score > c.strong_ratio else 0
        if not self.moved and (sum(self.window) >= c.k_of_n[0] or self.strong_streak >= c.strong_frames):
            self.moved = res.triggered = True
        res.moved = self.moved
        return res

    def _build_ref(self):
        """ท่าตั้งต้น = median ของหลายเฟรม (เดิมใช้เฟรมเดียว ซึ่งถ้าเฟรมนั้นสั่น ทั้งรอบจะเพี้ยนตาม)"""
        P = np.stack([b[0] for b in self.ref_buf])
        M = np.stack([b[1] for b in self.ref_buf])
        C = np.stack([b[2] for b in self.ref_buf])
        scales = [b[3] for b in self.ref_buf if b[3] is not None]
        if not scales:
            self.ref_buf = self.ref_buf[-self.cfg.ref_frames:]   # ยังไม่เห็นลำตัว เก็บต่อไปก่อน
            return
        self.ref = np.zeros((17, 2), np.float32)
        for j in range(17):
            if M[:, j].any():
                self.ref[j] = np.median(P[M[:, j], j], axis=0)
        self.ref_ok = (M.mean(axis=0) >= 0.5) & (np.median(C, axis=0) > self.cfg.vis_on)
        self.ref_scale = max(float(np.median(scales)), 10.0)
        self.mode = "judge"


# ---------------------------------------------------------------- old detector (เดิม)
OLD_PARTS = {
    "head": [0, 1, 2, 3, 4],
    "left_arm": [5, 7, 9],
    "right_arm": [6, 8, 10],
    "torso": [5, 6, 11, 12],
    "left_leg": [11, 13, 15],
    "right_leg": [12, 14, 16],
}


def old_pose_score(ref, cur, min_conf=0.5):
    """ก๊อปจาก part_scores + pose_score ใน test_motion.py"""
    (xy0, c0, h0), (xy1, c1, h1) = ref, cur
    h = max((h0 + h1) / 2, 1.0)
    ok = (c0 > min_conf) & (c1 > min_conf)
    d = np.linalg.norm(xy1 - xy0, axis=1) / h
    parts = {}
    for name, idx in OLD_PARTS.items():
        idx = [k for k in idx if ok[k]]
        if idx:
            parts[name] = float(d[idx].mean())
    if not parts:
        return None, None
    worst = max(parts, key=parts.get)
    return parts[worst], worst


class OldDetector:
    """ตรรกะเดิม: ท่าตั้งต้น = เฟรมแรกของไฟแดง, คะแนน = ค่าเฉลี่ยของส่วนที่ขยับมากที่สุด / ความสูงตัว,
    ต้องเกิน thr ติดกัน frames เฟรม"""
    name = "old"

    def __init__(self, thr=0.05, frames=3):
        self.thr, self.frames = thr, frames
        self.mode, self.ref, self.streak, self.moved = "idle", None, 0, False
        self.calib_info = {}

    def start_calibration(self):
        self.mode = "calib"

    def finish_calibration(self):
        self.mode = "idle"
        return {}

    def start_red(self):
        self.mode, self.ref, self.streak, self.moved = "judge", None, 0, False

    def stop(self):
        self.mode = "idle"

    def update(self, t, pose):
        res = MotionResult(mode=self.mode, moved=self.moved)
        if self.mode != "judge" or pose is None:
            return res
        if self.ref is None:
            self.ref = pose
        score, part = old_pose_score(self.ref, pose)
        if score is None:
            return res
        res.valid, res.part, res.raw, res.score = True, part, score, score / self.thr
        self.streak = self.streak + 1 if score > self.thr else 0
        if self.streak >= self.frames and not self.moved:
            self.moved = res.triggered = True
        res.moved = self.moved
        return res


def make_detector(kind="new", **kw):
    return OldDetector(**kw) if kind == "old" else MotionDetector(**kw)
