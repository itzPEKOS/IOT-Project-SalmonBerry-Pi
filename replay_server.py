"""
replay_server.py: รันบน laptop รับคลิปรีเพลย์จาก Pi ผ่าน HTTP แล้วเปิดหน้าให้ดูว่าผู้เล่นขยับตอนไหน

รัน:   python replay_server.py                  (ใช้ Flask ที่มากับ Anaconda)
เปิด:  http://localhost:5000                    รายการคลิปทุกรอบ
       http://localhost:5000/replay/<ชื่อ>       หน้าเล่นคลิป + timeline + ปุ่ม JUMP TO MOVE

API:
    POST /api/replay        form: round_id, files: video (.mp4/.webm), meta (.json)   ← Pi ส่งมา
    GET  /api/replays       รายการคลิป (json)
    GET  /replays/<file>    ไฟล์วิดีโอ / json (รองรับการเลื่อนดูในวิดีโอ)
ไฟล์เก็บไว้ที่โฟลเดอร์ replays/ ข้างไฟล์นี้
"""
import argparse
import json
import os
import socket

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")
os.makedirs(ROOT, exist_ok=True)
app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/api/replay")
def upload():
    v, m = request.files.get("video"), request.files.get("meta")
    if not v or not m:
        return jsonify(ok=False, error="need files: video, meta"), 400
    vname = secure_filename(v.filename) or "clip.mp4"
    base = os.path.splitext(vname)[0]
    if os.path.splitext(vname)[1].lower() not in (".mp4", ".webm"):
        return jsonify(ok=False, error="video must be .mp4 or .webm"), 400
    v.save(os.path.join(ROOT, vname))
    m.save(os.path.join(ROOT, base + ".json"))
    print(f"received replay: {vname}  (round {request.form.get('round_id')})")
    return jsonify(ok=True, name=base, url=f"/replays/{vname}", meta=f"/replays/{base}.json", page=f"/replay/{base}")


def list_meta():
    out = []
    for f in sorted(os.listdir(ROOT), reverse=True):
        if f.endswith(".json"):
            try:
                with open(os.path.join(ROOT, f), encoding="utf-8") as fp:
                    d = json.load(fp)
                d.pop("frames", None)
                d["name"] = f[:-5]
                out.append(d)
            except (OSError, ValueError):
                pass
    return out


@app.get("/api/replays")
def api_list():
    return jsonify(list_meta())


@app.get("/replays/<path:name>")
def files(name):
    return send_from_directory(ROOT, name, conditional=True)


STYLE = """<style>body{margin:0;background:#0E0F1F;color:#F4F1E8;font-family:system-ui,sans-serif;padding:24px}
a{color:#45D6FF}h1{font-size:22px;letter-spacing:1px}.muted{color:#8F93C9}
.row{display:flex;gap:12px;align-items:center;padding:10px 14px;background:#1A1C38;border:3px solid #05060F;margin-bottom:8px}
.badge{padding:4px 10px;font-weight:700;color:#0E0F1F}.WIN{background:#3BE36B}.GAME_OVER{background:#FF3B4E}
button{font-size:15px;padding:10px 16px;border:3px solid #05060F;background:#24275A;color:#F4F1E8;cursor:pointer;margin-right:8px}
button.red{background:#FF3B4E;color:#0E0F1F}</style>"""


@app.get("/")
def index():
    rows = "".join(
        f'<div class="row"><span class="badge {d.get("result") or ""}">{d.get("result") or "?"}</span>'
        f'<b>round {d.get("round_id")}</b><span>{d.get("cause") or ""} {d.get("part") or ""} {d.get("joint") or ""}</span>'
        f'<span class="muted">{d.get("duration", "?")} s · event at {d.get("moved_at")} s · {d.get("created", "")}</span>'
        f'<a href="/replay/{d["name"]}">▶ open</a></div>' for d in list_meta())
    return f"<!doctype html><meta charset=utf-8><title>Replays</title>{STYLE}<h1>REPLAYS</h1>" + (rows or '<p class="muted">ยังไม่มีคลิป</p>')


@app.get("/replay/<name>")
def player(name):
    name = secure_filename(name)
    if not os.path.exists(os.path.join(ROOT, name + ".json")):
        abort(404)
    return f"""<!doctype html><meta charset=utf-8><title>Replay {name}</title>{STYLE}
<p><a href="/">◀ all replays</a></p><h1 id="title">REPLAY</h1><p id="cause" class="muted"></p>
<video id="v" controls style="width:800px;max-width:100%;background:#000;border:4px solid #05060F"></video>
<div style="margin:10px 0"><button id="start">▶ FROM START</button><button class="red" id="jump">JUMP TO MOVE</button><button id="slow">SLOW-MO 0.25×</button><button id="normal">1×</button></div>
<canvas id="tl" width="800" height="150" style="max-width:100%;background:#0A0B17;border:3px solid #05060F"></canvas>
<p class="muted">แถบบน = สีไฟ · แท่ง = motion score (เส้นเหลือง = เกณฑ์ 1.0) · เส้นแดง = จังหวะที่เกิดเหตุ · เส้นขาว = ตำแหน่งที่กำลังเล่น · คลิกที่ timeline เพื่อกระโดดไปเวลานั้น</p>
<script>
const v=document.getElementById('v'),cv=document.getElementById('tl'),ctx=cv.getContext('2d');let meta=null;
const COL={{GREEN_LIGHT:'#3BE36B',TRANSITION:'#FFC93C',RED_LIGHT:'#FF3B4E',GAME_OVER:'#A31327',WIN:'#1E5A35',CALIBRATE:'#45D6FF'}};
fetch('/replays/{name}.json').then(r=>r.json()).then(m=>{{meta=m;v.src='/replays/'+m.video;
 document.getElementById('title').textContent=(m.result||'')+' · ROUND '+m.round_id;
 document.getElementById('cause').textContent=[m.cause,m.part&&(m.part+' / '+m.joint),m.score&&('score '+m.score),m.moved_at!=null&&('event at '+m.moved_at+' s'),'length '+m.duration+' s'].filter(Boolean).join(' · ');
 if(m.result==='WIN')document.getElementById('jump').textContent='JUMP TO FINISH';
 if(m.moved_at==null)document.getElementById('jump').style.display='none';
 draw();}});
const X=t=>t/meta.duration*cv.width;
function draw(){{if(!meta)return;ctx.clearRect(0,0,cv.width,cv.height);
 const fr=meta.frames;for(let i=0;i<fr.length;i++){{const a=fr[i].t,b=i+1<fr.length?fr[i+1].t:meta.duration;ctx.fillStyle=COL[fr[i].state]||'#3A3F86';ctx.fillRect(X(a),0,X(b)-X(a)+1,18);
  if(fr[i].score!=null){{const h=Math.min(fr[i].score,2.5)/2.5*110;ctx.fillStyle=fr[i].score>1?'#FF3B4E':'#3BE36B';ctx.fillRect(X(a),140-h,Math.max(2,X(b)-X(a)-1),h);}}}}
 ctx.fillStyle='#FFC93C';for(let x=0;x<cv.width;x+=10)ctx.fillRect(x,140-110/2.5,6,2);
 if(meta.moved_at!=null){{ctx.fillStyle='#FF3B4E';ctx.fillRect(X(meta.moved_at)-1,0,3,cv.height);ctx.font='bold 13px monospace';ctx.fillText((meta.result==='WIN'?'FINISH ':'MOVED ')+meta.moved_at+'s',Math.min(X(meta.moved_at)+6,cv.width-110),34);}}
 ctx.fillStyle='#F4F1E8';ctx.fillRect(X(v.currentTime||0)-1,0,2,cv.height);}}
v.addEventListener('timeupdate',draw);setInterval(draw,100);
document.getElementById('start').onclick=()=>{{v.currentTime=0;v.playbackRate=1;v.play();}};
document.getElementById('jump').onclick=()=>{{v.currentTime=Math.max(0,meta.moved_at-0.5);v.playbackRate=0.25;v.play();}};
document.getElementById('slow').onclick=()=>v.playbackRate=0.25;document.getElementById('normal').onclick=()=>v.playbackRate=1;
cv.onclick=e=>{{const r=cv.getBoundingClientRect();v.currentTime=(e.clientX-r.left)/r.width*meta.duration;}};
</script>"""


def free_port(preferred):
    for p in [preferred, 5050, 5500, 8090, 8181, 9000]:
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            print(f"port {p} ใช้ไม่ได้ ลอง port ถัดไป")
        finally:
            s.close()
    raise SystemExit("หา port ว่างไม่เจอ ระบุเองด้วย --port")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    port = free_port(args.port)
    print(f"replay server: http://localhost:{port}   (ไฟล์อยู่ที่ {ROOT})")
    if port != 5000:
        print(f"** port ไม่ใช่ 5000: รัน pi_client.py ด้วย --replay-url http://localhost:{port}/api/replay")
    app.run(host="0.0.0.0", port=port, threaded=True)
