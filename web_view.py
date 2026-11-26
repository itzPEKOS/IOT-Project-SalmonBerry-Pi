"""
web_view.py: เปิดหน้าเว็บดูกล้องสด + โครงร่าง + ผลตรวจการขยับ (ไม่ต้องต่อจอกับ Pi)

รันบน Pi:
    source ~/rlgl-venv/bin/activate
    python web_view.py                    # กล้อง CSI (Pi Camera)
    python web_view.py --source 0         # กล้อง USB
    python web_view.py --no-pose          # ดูภาพกล้องอย่างเดียว ไม่รันโมเดล (เช็กกล้อง/มุมกล้อง)
แล้วเปิดเบราว์เซอร์บน laptop (ต่อ Wi-Fi/hotspot วงเดียวกับ Pi):
    http://<IP ของ Pi>:8000          เช่น http://172.20.10.2:8000

บน laptop ก็ใช้ได้: python web_view.py --source 0 แล้วเปิด http://localhost:8000

ปุ่มบนเว็บ (หรือคีย์บอร์ด): SPACE = ไฟเขียว/แดง, C = calibrate, + / - = ความไว (k)
ใช้แค่ไลบรารีที่มีอยู่แล้ว (OpenCV, MediaPipe, picamera2) และไฟล์ในโปรเจกต์: pi_detect.py, test_motion.py, motion_detector.py
"""
import argparse
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2

from motion_detector import DetectorConfig, MotionDetector, OldDetector
from pi_detect import Camera, cpu_temp
from test_motion import MediaPipePose, ResourceMonitor, draw_mp_all, put

PAGE = """<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RLGL camera</title>
<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee}
 .wrap{display:flex;flex-wrap:wrap;gap:16px;padding:16px}
 img{max-width:100%;width:720px;border-radius:8px;background:#000}
 .panel{flex:1;min-width:280px;max-width:420px}
 .state{font-size:32px;font-weight:700;padding:12px 16px;border-radius:8px;margin-bottom:12px;background:#333}
 .GREEN{background:#1b7f3a}.GRACE{background:#b7791f}.RED,.CAUGHT{background:#b91c1c}.CALIB{background:#2563eb}
 button{font-size:16px;padding:10px 14px;margin:0 6px 8px 0;border:0;border-radius:6px;background:#444;color:#fff;cursor:pointer}
 button:hover{background:#555}
 table{width:100%;border-collapse:collapse;font-size:14px}td{padding:3px 0}td:last-child{text-align:right}
 .bar{height:12px;background:#333;border-radius:3px;position:relative;margin:2px 0 8px}
 .bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
 .bar b{position:absolute;left:50%;top:-2px;bottom:-2px;width:2px;background:#ddd}
 .err{color:#f87171;white-space:pre-wrap;font-size:13px}.muted{color:#999;font-size:13px}
</style></head><body><div class="wrap">
<img id="cam" src="/stream" alt="camera">
<div class="panel">
 <div id="state" class="state">...</div>
 <div id="result" style="font-size:18px;margin-bottom:10px"></div>
 <button onclick="cmd('toggle')">SPACE ไฟเขียว/แดง</button>
 <button onclick="cmd('calib')">C Calibrate</button>
 <button onclick="cmd('k-')">− ไวขึ้น</button><button onclick="cmd('k+')">+ ไวน้อยลง</button>
 <table id="info"></table>
 <div style="margin-top:10px">คะแนนแต่ละส่วน (เส้นกลาง = เกณฑ์)</div><div id="parts"></div>
 <div id="err" class="err"></div>
 <p class="muted">calibrate: ยืนนิ่งเห็นทั้งตัว 5 วินาที แล้วค่อยเริ่มไฟแดง</p>
</div></div>
<script>
const PARTS=["head","left_arm","right_arm","torso","left_leg","right_leg","upper_body"];
function cmd(c){fetch('/cmd?c='+encodeURIComponent(c))}
document.addEventListener('keydown',e=>{if(e.code==='Space'){e.preventDefault();cmd('toggle')}
 else if(e.key==='c'||e.key==='C')cmd('calib');else if(e.key==='+'||e.key==='=')cmd('k+');else if(e.key==='-')cmd('k-')});
function row(k,v){return `<tr><td>${k}</td><td>${v??'-'}</td></tr>`}
async function poll(){try{const s=await (await fetch('/status')).json();
 const st=document.getElementById('state');st.textContent=s.state+(s.state==='CALIB'?` ${s.calib_left}s`:'');st.className='state '+s.state;
 document.getElementById('result').textContent=s.result||'';
 document.getElementById('info').innerHTML=row('FPS',s.fps)+row('inference (ms)',s.infer_ms)+row('detector',s.detector+' k='+s.k)
  +row('score (1.0 = เกณฑ์)',s.score)+row('จุดที่ขยับมากสุด',s.joint?`${s.part} / ${s.joint}`:null)+row('จุดที่เห็น',s.n_joints)
  +row('calibrate',s.calib)+row('CPU / RAM',`${s.cpu}% / ${s.ram} MB`)+row('อุณหภูมิ',s.temp?s.temp+' °C':null);
 let h='';for(const p of PARTS){const v=s.parts[p];if(v===undefined)continue;const w=Math.min(v/2,1)*100;
  h+=`<div style="font-size:13px">${p} ${v.toFixed(2)}</div><div class="bar"><i style="width:${w}%;background:${v>1?'#ef4444':'#22c55e'}"></i><b></b></div>`}
 document.getElementById('parts').innerHTML=h||'<div class="muted">แสดงตอนไฟแดง</div>';
 document.getElementById('err').textContent=s.error||'';
}catch(e){}setTimeout(poll,300)}
poll();
</script></body></html>"""


class App:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.new_frame = threading.Condition(self.lock)
        self.jpg, self.seq = None, 0
        self.cmds = []
        self.status = {"state": "STARTING", "parts": {}, "error": ""}
        self.det = (OldDetector(thr=args.thr) if args.detector == "old"
                    else MotionDetector(DetectorConfig(k_sigma=args.k)))

    def command(self, c):
        with self.lock:
            self.cmds.append(c)

    def publish(self, frame, status):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.args.quality])
        with self.lock:
            if ok:
                self.jpg, self.seq = buf.tobytes(), self.seq + 1
            self.status = status
            self.new_frame.notify_all()

    def set_error(self, msg):
        with self.lock:
            self.status = {**self.status, "state": "ERROR", "error": msg}
            self.new_frame.notify_all()

    # ------------------------------------------------------------ camera + model loop
    def run(self):
        a, det = self.args, self.det
        try:
            cam = Camera(a.source, a.width, a.height)
        except Exception as e:
            self.set_error(f"เปิดกล้องไม่ได้: {e}\n\nเช็ก: rpicam-hello --list-cameras (กล้อง CSI)\n"
                           f"หรือใช้กล้อง USB: python web_view.py --source 0")
            return
        pose = None
        if not a.no_pose:
            try:
                pose = MediaPipePose(a.variant)
            except BaseException as e:
                self.set_error(f"โหลด MediaPipe ไม่ได้: {e}")
                return
        mon = ResourceMonitor()
        is_new = isinstance(det, MotionDetector)
        state, t_state, result, calib_txt = "GREEN", time.perf_counter(), "", "ยังไม่ได้ทำ"
        if pose is not None and is_new and a.calib > 0:
            state = "CALIB"
            det.start_calibration()
        fps, infer_ms, t_prev, t_log = 0.0, 0.0, time.perf_counter(), 0.0
        temp = None
        try:
            while True:
                frame = cam.read()
                if frame is None:
                    self.set_error("อ่านภาพจากกล้องไม่ได้")
                    time.sleep(0.5)
                    continue
                t0 = time.perf_counter()
                cur = pose(frame) if pose is not None else None
                dt = (time.perf_counter() - t0) * 1000
                infer_ms = 0.9 * infer_ms + 0.1 * dt if infer_ms else dt
                now = time.perf_counter()

                # --- คำสั่งจากหน้าเว็บ
                with self.lock:
                    cmds, self.cmds = self.cmds, []
                for c in cmds:
                    if pose is None:
                        continue
                    if c == "toggle" and state != "CALIB":
                        if state == "GREEN":
                            state, t_state, result = "GRACE", now, ""
                        else:
                            state, result = "GREEN", ""
                            det.stop()
                    elif c == "calib" and is_new and state in ("GREEN", "CALIB"):
                        state, t_state = "CALIB", now
                        det.start_calibration()
                    elif c in ("k+", "k-"):
                        if is_new:
                            det.cfg.k_sigma = max(round(det.cfg.k_sigma + (0.25 if c == "k+" else -0.25), 2), 1.0)
                            det._qcache.clear()
                        else:
                            det.thr = max(round(det.thr + (0.005 if c == "k+" else -0.005), 3), 0.005)

                # --- state machine
                if state == "CALIB" and now - t_state >= a.calib:
                    info = det.finish_calibration()
                    calib_txt = (f"{info.get('frames')} เฟรม, noise x{info.get('noise_factor')}"
                                 + (f" ({info['msg']})" if info.get("msg") else ""))
                    state = "GREEN"
                if state == "GRACE" and now - t_state >= a.grace and cur is not None:
                    state = "RED"
                    det.start_red()
                r = det.update(now, cur) if pose is not None else None
                if state == "RED" and r is not None and r.triggered:
                    state = "CAUGHT"
                    result = f"MOVED: {r.part} / {r.joint}  (score {r.score:.2f})"

                # --- ภาพ
                inst = 1.0 / max(now - t_prev, 1e-6)
                fps = 0.9 * fps + 0.1 * inst if fps else inst
                t_prev = now
                mon.update()
                if now - t_log > 2:
                    t_log, temp = now, cpu_temp()
                vis = frame.copy()
                if pose is not None:
                    draw_mp_all(vis, pose.all_points)
                color = {"GREEN": (0, 200, 0), "GRACE": (0, 200, 255), "CALIB": (255, 160, 0)}.get(state, (0, 0, 255))
                put(vis, f"{state}  FPS {fps:4.1f}", 28, color)
                if result:
                    put(vis, result, 56, (0, 0, 255))
                elif pose is not None and cur is None:
                    put(vis, "no player detected", 56, (0, 200, 255))

                show_score = r is not None and state in ("RED", "CAUGHT") and r.score is not None
                k_val = det.cfg.k_sigma if is_new else det.thr
                self.publish(vis, {
                    "state": state if pose is not None else "CAMERA ONLY",
                    "calib_left": round(max(a.calib - (now - t_state), 0), 1),
                    "result": result, "error": "",
                    "fps": round(fps, 1), "infer_ms": round(infer_ms, 1),
                    "detector": a.detector, "k": k_val,
                    "score": round(r.score, 2) if show_score else None,
                    "part": r.part if show_score else None, "joint": r.joint if show_score else None,
                    "n_joints": r.n_joints if r is not None and r.mode == "judge" else None,
                    "parts": {k: round(v, 3) for k, v in getattr(det, "last_parts", {}).items()}
                    if state in ("RED", "CAUGHT") else {},
                    "calib": calib_txt if is_new else "-",
                    "cpu": round(mon.cpu, 1), "ram": round(mon.ram), "temp": temp,
                })
        except Exception:
            self.set_error(traceback.format_exc())
        finally:
            cam.close()
            if pose is not None:
                pose.landmarker.close()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif u.path == "/status":
                with app.lock:
                    s = dict(app.status)
                self._send(200, "application/json", json.dumps(s, ensure_ascii=False).encode())
            elif u.path == "/cmd":
                app.command(parse_qs(u.query).get("c", [""])[0])
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with app.new_frame:
                            app.new_frame.wait_for(lambda: app.seq != last, timeout=2.0)
                            jpg, last = app.jpg, app.seq
                        if jpg is None:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

    return Handler


def local_ips():
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips) or ["<IP ของเครื่องนี้>"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="picam", help='"picam" = กล้อง CSI, หรือเลข index ของกล้อง USB')
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--variant", choices=["lite", "full", "heavy"], default="lite")
    ap.add_argument("--no-pose", action="store_true", help="แสดงภาพกล้องอย่างเดียว")
    ap.add_argument("--detector", choices=["new", "old"], default="new")
    ap.add_argument("--k", type=float, default=4.5)
    ap.add_argument("--thr", type=float, default=0.05, help="สำหรับ --detector old")
    ap.add_argument("--calib", type=float, default=5.0)
    ap.add_argument("--grace", type=float, default=0.7)
    ap.add_argument("--quality", type=int, default=70, help="คุณภาพ JPEG ที่ส่งไปเว็บ")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    app = App(args)
    threading.Thread(target=app.run, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(app))
    server.daemon_threads = True
    print("เปิดเบราว์เซอร์ที่:")
    for ip in local_ips():
        print(f"    http://{ip}:{args.port}")
    print(f"    http://localhost:{args.port}  (ถ้าเปิดบนเครื่องเดียวกัน)")
    print("หยุดด้วย Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
