# เว็บเกม: backend และ frontend

```
web/
├── backend/    FastAPI
├── frontend/   หน้าเว็บเกม (HTML/CSS/JS ไม่ต้อง build) ดู frontend/README.md
├── tests/      เทสต์ของ backend (pytest) และ tests/frontend/ (node --test)
├── clips/      วิดีโอ เฉพาะตอนยังไม่ได้ตั้ง Supabase (สร้างเอง, ไม่ commit)
└── data/       ฐานข้อมูล SQLite: ชื่อผู้เล่นและผลแต่ละรอบ (สร้างเอง, ไม่ commit)
```

## เปิดทั้งหมดด้วยคำสั่งเดียว

ดับเบิลคลิก `run.cmd` ในโฟลเดอร์นี้ หรือรันใน PowerShell (จากโฟลเดอร์ `laptop/web`):

```powershell
.\run.cmd
```

| คำสั่ง | ใช้เมื่อ |
|---|---|
| `.\run.cmd` | ใช้งานปกติ เปิดเฉพาะเครื่องนี้ แล้วเปิด browser เป็นจอผู้เล่นให้ |
| `.\run.cmd -Lan` | วันรวมระบบ ให้ Pi ส่งวิดีโอเข้ามาและเครื่องอื่นเปิดดูได้ (บอก IP ที่ต้องใช้ให้) |
| `.\run.cmd -Sim` | ลองหน้าเว็บด้วยตัวจำลอง **แยกขาดจากของจริง**: ไม่ต่อ MQTT broker, ไม่อัปวิดีโอขึ้น cloud, ฐานข้อมูล `data/dev.db`, คลิป `data/dev-clips/` |
| `.\run.cmd -Dev` | ฐานข้อมูลแยก + เปิด `/dev/publish` แต่**ยังต่อ broker** ใช้ตอนรันตัวจำลองผ่าน MQTT เองเพื่อดู Grafana |

ตัวเลือกเพิ่ม: `-Port 8001`, `-Rounds 10` (จำนวนรอบของตัวจำลอง), `-NoBrowser` กด Ctrl+C เพื่อปิด (ตัวจำลองปิดตามไปด้วย)

สคริปต์ตรวจให้ก่อนเปิด: มี Python และ package ครบไหม (ถ้าไม่ครบจะบอกคำสั่งติดตั้ง ไม่ติดตั้งเอง), มี ffmpeg ไหม, port ว่างไหม, ต่อ MQTT broker ได้ไหม
สคริปต์ไม่เปิด Docker ให้ ต้องเปิด stack ใน `../infra` เองก่อน (ดู [`../README.md`](../README.md))

`run.cmd` เรียก `run.ps1` แบบข้าม execution policy เฉพาะครั้งนั้น ถ้าเครื่องอนุญาตสคริปต์อยู่แล้ว รัน `.\run.ps1` ตรง ๆ ได้

`-Sim` ตั้ง `RLGL_ISOLATED=1` ให้ backend ซึ่งไม่สนค่า broker และ Supabase ใน `.env` และสคริปต์จะไม่เปิดตัวจำลองถ้า backend ไม่ได้อยู่ในโหมดนี้ ข้อมูลใน `data/dev.db` และ `data/dev-clips/` ไม่ถูกลบเอง ลบโฟลเดอร์เหล่านี้ได้เมื่อไม่ใช้แล้ว
ถ้ารัน `.\run.ps1` ในหน้าต่าง PowerShell เดิม ค่าที่สคริปต์เปลี่ยนจะถูกคืนเมื่อปิด จึงไม่ค้างโหมดทดลองไปรอบถัดไป

## เปิดหน้าเว็บ

backend เปิดหน้าเว็บให้เอง ไม่ต้องรัน server แยก

| ที่อยู่ | ใช้กับ |
|---|---|
| http://localhost:8000/ | **จอที่ผู้เล่นดู** (ต่อแบบ `role=display` ทำให้ game engine รู้ว่าจอยังอยู่) เปิดเครื่องเดียวเท่านั้น |
| http://localhost:8000/?role=viewer | จอคนดู คนคุม หรือเครื่องอื่น (ไม่นับเป็นจอผู้เล่น) |

- จอผู้เล่นกดแท็บ SCOREBOARD ไม่ได้ระหว่าง calibrate และระหว่างเล่น และจะกลับมาหน้าเกมเองเมื่อรอบเริ่ม
- ถ้าหลุดจาก backend จะขึ้นจอ DISCONNECTED ทับทั้งหน้า (ไม่ค้างสีเขียวหรือแดง) และต่อใหม่เอง

ลองหน้าเว็บโดยไม่ใช้ broker (ส่งข้อมูลจำลองเข้า backend โดยตรง):

```powershell
$env:RLGL_DEV = "1"; $env:RLGL_DB_PATH = "data/test.db"; python -m uvicorn backend.app:make_app --factory --port 8000
```

```powershell
python ../sim/simulate.py --via-backend http://127.0.0.1:8000 --mode follow --rounds 3
```

`--mode follow` เล่นโหมดที่เลือกไว้บนหน้าเว็บ (RED LIGHT หรือ SNIPER) ใช้ `--mode sniper --scenario hits` เพื่อบังคับได้

## รัน backend

ต้องเปิด service ใน `../infra` ก่อน (ดู [`../README.md`](../README.md)) แล้วรันจากโฟลเดอร์ `laptop/web`

```powershell
pip install -r backend/requirements.txt
```

```powershell
python -m uvicorn backend.app:make_app --factory --host 127.0.0.1 --port 8000
```

ตรวจว่าทำงาน: http://localhost:8000/api/health และดูเอกสาร API ที่ http://localhost:8000/docs

ให้ Pi ส่งวิดีโอเข้ามาได้ ต้องใช้ `--host 0.0.0.0` และอนุญาต port 8000 ใน Windows Firewall เฉพาะเครือข่ายของกลุ่ม

## วิดีโอใน Supabase Storage

Pi ส่ง JPEG ทุกเฟรมของรอบมาครั้งเดียวหลังจบรอบ backend แปลงเป็น MP4 ด้วย ffmpeg อัปขึ้น Supabase แล้วลบไฟล์ในเครื่องทันที (รายละเอียดอยู่ใน[ข้อตกลง MQTT](../../docs/mqtt-contract.md))

ตั้งค่าครั้งแรก (คนในทีมทำเอง):

1. สมัคร https://supabase.com แล้วสร้างโปรเจกต์
2. Storage → New bucket ชื่อ `clips` **ไม่ติ๊ก Public** (คลิปมีหน้าคน)
3. Project Settings → API Keys: คัดลอก Project URL และ secret key (`sb_secret_...`) หรือ `service_role` key
4. ใส่ใน `laptop/infra/.env` บน laptop เครื่องที่รัน backend แล้ว restart backend

```
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_KEY=<secret key>
SUPABASE_BUCKET=clips
```

- **ห้ามใส่ key ในหน้าเว็บ บน Pi หรือ commit** key นี้อ่าน เขียน และลบได้ทุกอย่างในโปรเจกต์
- หน้าเว็บเปิด `/clips/<round_id>.mp4` บน laptop แล้ว backend ส่งต่อไปยังลิงก์ชั่วคราวของ Supabase (อายุ `RLGL_CLIP_URL_TTL_S` วินาที) วิดีโอจึงโหลดจาก cloud โดยตรง ต้องมี internet ทั้ง laptop และเครื่องที่เปิดดู
- ถ้าอัปไม่ได้ (ไม่มี internet, key ผิด, bucket ไม่มี) backend ตอบ `503` ให้ Pi เก็บไฟล์ไว้ส่งใหม่ รายละเอียดอยู่ใน log ของ backend
- ถ้าไม่ตั้งสามค่านี้ backend จะเก็บวิดีโอใน `clips/` บน laptop แทน (ใช้ตอนพัฒนา)
- laptop ต้องมี **ffmpeg** ใน PATH

ส่งข้อมูลจำลองพร้อมคลิปทดสอบ (ต้องมี ffmpeg ใน PATH) **ไม่ต้องใส่ `--fake-backend`**

```powershell
python ../sim/simulate.py --rounds 3 --backend http://127.0.0.1:8000
```

รันเทสต์ (backend และส่วนคำนวณของหน้าเว็บ ต้องมี Node.js 18 ขึ้นไป):

```powershell
python -m pytest
```

```powershell
node --test tests/frontend/logic.test.mjs
```

## ตั้งค่า (environment variable หรือใส่ใน `../infra/.env`)

| ค่า | ค่าเริ่มต้น | ใช้ทำอะไร |
|---|---|---|
| `RLGL_MQTT_HOST`, `RLGL_MQTT_PORT` | `127.0.0.1`, `1883` | ที่อยู่ broker (ตอนรวมระบบใส่ IP ของ Pi) ใส่ค่าว่างเพื่อไม่ต่อ MQTT |
| `MQTT_USER`, `MQTT_PASSWORD` | อ่านจาก `.env` | บัญชี MQTT |
| `RLGL_DB_PATH` | `data/rlgl.db` | ไฟล์ฐานข้อมูล ใช้ไฟล์อื่นตอนลองกับข้อมูลจำลอง จะได้ไม่ปนกับของจริง |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET` | ไม่ตั้ง, ไม่ตั้ง, `clips` | เก็บวิดีโอใน Supabase Storage (ดูหัวข้อข้างบน) |
| `RLGL_CLIP_URL_TTL_S` | `3600` | อายุลิงก์ชั่วคราวของวิดีโอใน cloud (วินาที) |
| `RLGL_FFMPEG` | `ffmpeg` | ที่อยู่ของ ffmpeg ถ้าไม่อยู่ใน PATH |
| `RLGL_CLIPS_DIR` | `clips/` | โฟลเดอร์เก็บวิดีโอ เมื่อไม่ได้ตั้ง Supabase |
| `RLGL_UPLOAD_TOKEN` | ไม่ตั้ง | ถ้าตั้ง การอัปโหลดต้องส่ง header `X-Upload-Token` |
| `RLGL_MAX_UPLOAD_MB` | `300` | ขนาดวิดีโอสูงสุด |
| `RLGL_PUBLIC_BASE_URL` | ตามที่อยู่ที่ Pi เรียกเข้ามา | ใช้เมื่อลิงก์วิดีโอต้องเป็นที่อยู่อื่น |
| `RLGL_CORS_ORIGINS` | ไม่ตั้ง | ที่อยู่ dev server ของ frontend คั่นด้วย `,` เช่น `http://localhost:5173` |
| `RLGL_DEV` | ไม่ตั้ง | `1` = เปิด `POST /dev/publish` (เรียกได้จากเครื่องนี้เท่านั้น) |
| `RLGL_STREAM_URL` | ไม่ตั้ง | ภาพสดจาก Pi (MJPEG ผ่าน HTTP) เช่น `http://<IP ของ Pi>:8080/stream.mjpg` หน้าเว็บดึงตรงจาก Pi ใช้ในหน้า calibrate และ Sniper |
| `RLGL_GRAFANA_URL` | `http://<เครื่องนี้>:3000/d/rlgl-main` | ปุ่ม OPEN GRAFANA ในหน้ารายละเอียดรอบ |
| `RLGL_TRACK_CM` | `300` | ระยะจาก ultrasonic ถึงเส้นเริ่ม (cm) ใช้คิดว่าเดินมาแล้วกี่เมตร |

# ข้อมูลสำหรับคนทำ frontend

หน้าเว็บคุยกับ backend อย่างเดียว **ไม่ต้องต่อ MQTT เอง** และไม่ต้องมีรหัส MQTT ใน browser

## ข้อมูลสด: WebSocket `/ws`

| ที่อยู่ | ใช้กับ |
|---|---|
| `ws://<host>:8000/ws?role=display` | **จอที่ผู้เล่นดู** (ไฟเขียว/แดง) |
| `ws://<host>:8000/ws` | จอของคนดูหรือคนคุม |

**กติกาของจอผู้เล่น:** ต้องส่งข้อความอะไรก็ได้ (เช่น `ping`) **ทุก 2 วินาที** ถ้าเงียบเกิน 6 วินาที backend จะตัดการเชื่อมต่อและประกาศว่าจอ offline

ข้อความแรกคือ snapshot (ค่าล่าสุดของแต่ละ topic) หลังจากนั้นเป็นทีละข้อความ

```json
{"type": "snapshot", "messages": {"rlgl/game/state": {"round_id": "r12", "state": "GREEN_LIGHT", "remaining_s": 41}}}
```

```json
{"type": "message", "topic": "rlgl/gateway/distance", "payload": {"distance_cm": 182.4}}
```

โค้ดตั้งต้น:

```js
const latest = {};
let heartbeat;

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws?role=display`);
  socket.onopen = () => { heartbeat = setInterval(() => socket.send("ping"), 2000); };
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot") {
      for (const key of Object.keys(latest)) delete latest[key];
      Object.assign(latest, data.messages);
    } else {
      if (data.topic === "rlgl/game/state" && latest[data.topic]?.round_id !== data.payload.round_id) {
        delete latest["rlgl/game/result"];
        delete latest["rlgl/game/summary"];
        delete latest["rlgl/game/media"];
      }
      latest[data.topic] = data.payload;
    }
    render(latest);
  };
  socket.onclose = () => {
    clearInterval(heartbeat);
    showDisconnected();          // ต้องเห็นชัด: ห้ามค้างสีเขียวหรือแดงเดิมไว้
    setTimeout(connect, 1000);
  };
}
```

ตัวอย่างนี้ยังไม่ได้ทดสอบใน browser และ `render`, `showDisconnected` ต้องเขียนเอง

## topic ที่หน้าเว็บใช้

| topic | field ที่ใช้ | แสดงอะไร |
|---|---|---|
| `rlgl/game/state` | `round_id`, `state`, `remaining_s` | สีจอและเวลาที่เหลือ |
| `rlgl/game/result` | `round_id`, `result`, `reason`, `duration_s` | ผลแพ้ชนะและสาเหตุ |
| `rlgl/game/summary` | `round_id`, `bpm_avg`, `bpm_max`, `red_motion_max`, `red_acc_peak_max`, `red_light_count`, `distance_cm`, `finish_state`, `closest_call` | สรุปรอบ (field ที่ไม่มีข้อมูลจะไม่ถูกส่งมา) |
| `rlgl/game/media` | `round_id`, `status`, `video_url` | วิดีโอหลังจบรอบ **ต้องเช็กว่า `round_id` ตรงกับผลที่กำลังแสดง** |
| `rlgl/gateway/distance` | `distance_cm` | progress bar |
| `rlgl/wrist/heartrate` | `bpm` | ชีพจร (ถ้าไม่มีค่าใหม่เกิน 10 วินาทีให้แสดง `--`) |
| `rlgl/status/<device_id>` | `status` | อุปกรณ์ online/offline |

topic อื่นใน [`docs/mqtt-contract.md`](../../docs/mqtt-contract.md) ก็ส่งมาด้วย ให้ข้าม topic ที่ไม่ได้ใช้

## สีจอผู้เล่นตาม `state`

| `state` | จอ |
|---|---|
| `IDLE`, `CALIBRATE` | สีเทา |
| `GREEN_LIGHT` | สีเขียว |
| `TRANSITION`, `RED_LIGHT` | สีแดง |
| `GAME_OVER` | แพ้ แบบนิ่ง ไม่กะพริบ พร้อมสาเหตุ |
| `WIN` | ชนะ |

`reason`: `touch`, `camera`, `imu`, `both`, `timeout`, `finish_during_red`, `disconnect`, `camera_lost`

## HTTP

| คำขอ | ได้อะไร |
|---|---|
| `GET /api/health` | `status`, `mqtt_connected`, `displays` |
| `GET /api/state` | ค่าล่าสุดของแต่ละ topic |
| `GET /api/clips` | รายการวิดีโอ (ใหม่สุดก่อน) |
| `GET /clips/<round_id>.mp4` | วิดีโอ รองรับการเลื่อนตำแหน่ง (ถ้าตั้ง Supabase จะได้ `307` ไปยังลิงก์ชั่วคราวของ cloud) |
| `POST /clips` | ใช้โดย Pi (ดูข้อตกลง MQTT) |

ถ้าวางไฟล์ใน `frontend/` โดยมี `index.html` backend จะเปิดให้ที่ http://localhost:8000/ (ต้อง restart backend ครั้งแรก)

## HTTP: ผู้เล่น ประวัติ และอันดับ

| คำขอ | ได้อะไร |
|---|---|
| `GET /api/session` | `player` (คนที่จะเล่นรอบถัดไป หรือ `null`, ตัวเลขเป็นของโหมดที่เลือก), `next_round_number`, `mode` |
| `POST /api/session` ส่ง `{"name":"BERRY"}` | เลือกชื่อ (สร้างใหม่ถ้ายังไม่มี) ได้ข้อมูลแบบเดียวกับข้างบน |
| `DELETE /api/session` | เลิกใช้ชื่อ รอบถัดไปไม่มีชื่อ |
| `POST /api/mode` ส่ง `{"mode":"rlgl"}` หรือ `"sniper"` | เลือกโหมด backend publish `rlgl/game/mode` (retained) ให้ game engine |
| `GET /api/config` | `stream_url`, `grafana_url`, `track_cm` |
| `GET /api/players?limit=4` | ผู้เล่นล่าสุด `{"players":[{"name"}]}` |
| `GET /api/players/<name>?mode=rlgl` | `name`, `rounds_played`, `wins`, `best_s`, `rank` (ใช้กับกล่อง Welcome back) |
| `GET /api/leaderboard?limit=7&mode=rlgl` | `entries`: `rank`, `player`, `duration_s`, `bpm_max`, `round_id` (อันดับแยกตามโหมด) |
| `GET /api/rounds?result=all\|win\|loss&page=1&page_size=7&mode=` | `rounds`, `page`, `page_size`, `total`, `counts` (`all`, `wins`, `losses`), `last_ended_at` (ไม่ใส่ `mode` = ทุกโหมด) |
| `GET /api/rounds/<round_id>` | ข้อมูลรอบเดียว และ `standing` (เทียบเฉพาะโหมดเดียวกัน) |

- ชื่อใช้ได้ 1-10 ตัว: `A-Z 0-9 _ -` เก็บเป็นตัวพิมพ์ใหญ่ ชื่อภาษาไทยใช้ไม่ได้
- หนึ่งรอบมี `number` (เลข `#07`), `round_id`, `mode` (`rlgl` / `sniper`), `hits`, `dodges` (เฉพาะ sniper), `player`, `started_at`, `ended_at` (epoch ms ตามนาฬิกา laptop), `duration_s`, `result`, `reason`, `finish_state`, `distance_cm`, `bpm_avg`, `bpm_max`, `bpm_rest`, `red_light_count`, `red_motion_max`, `red_acc_peak_max`, `closest_call`, `caught_part`, `caught_red_s`, `caught_score_ratio`, `video_url` ค่าที่ไม่มีข้อมูลเป็น `null`
- `bpm_rest`, `closest_call`, `caught_*` จะเป็น `null` จนกว่า Pi จะส่ง field ที่เสนอไว้ใน[ข้อตกลง MQTT](../../docs/mqtt-contract.md) (ตัวจำลองยังไม่ส่ง)
- `closest_call` ใช้ `score_ratio` หรือ `score` (ชื่อที่ `pi_client.py` ส่ง) และ `red_motion_max` ใช้ `motion_score` หรือ `raw`
- `standing` มีเฉพาะรอบที่ชนะและมีชื่อ: `rank`, `personal_best`, `new_record`, `beaten` (`{"player","by_s"}` หรือ `null`) **คิดจากสถานการณ์ตอนจบรอบนั้น** ส่วน `rank` ใน `/api/players` และ `/api/leaderboard` คืออันดับปัจจุบัน
- อันดับใช้เวลาดีที่สุดคนละหนึ่งแถว เวลาเท่ากันคนที่ทำได้ก่อนอยู่บน รอบที่ไม่มีชื่อไม่นับ
- ประวัติแสดงเฉพาะ `WIN` และ `GAME_OVER` รอบที่ถูกยกเลิก (`ABORTED`) ใช้เลขรอบไปแล้ว เลขในตารางประวัติจึงอาจข้าม
- ขอข้อมูลรอบได้หลังได้รับ `rlgl/game/result` ทาง `/ws` (บันทึกลงฐานข้อมูลก่อนที่ข้อความจะถูกส่งออก) ส่วน `video_url` มาหลังได้รับ `rlgl/game/media`

### backend บันทึกรอบอย่างไร

- สร้างรอบเมื่อเห็น `round_id` ใหม่ใน state `CALIBRATE`, `GREEN_LIGHT`, `TRANSITION`, `RED_LIGHT` หรือ `SNIPER` และผูกกับชื่อที่เลือกอยู่ตอนนั้น
- โหมดของรอบมาจาก field `mode` ใน `rlgl/game/state` ถ้า game engine ไม่ส่งมา ใช้โหมดที่เลือกบนหน้าเว็บตอนรอบเริ่ม
- เลือกชื่อใหม่ระหว่าง `CALIBRATE` ใช้กับรอบนั้น ถ้าเริ่มเล่นแล้วใช้กับรอบถัดไป
- `WIN`, `GAME_OVER`, `IDLE` ไม่สร้างรอบ เพราะ `rlgl/game/state` เป็น retained อาจเป็นของรอบเก่าที่ broker ส่งซ้ำ
- ผลแรกของแต่ละรอบเท่านั้นที่ถูกเก็บ **game engine ต้องไม่ใช้ `round_id` ซ้ำ** แม้ปิดเปิดใหม่ (ถ้าซ้ำ รอบใหม่จะไม่ถูกบันทึก และ log ของ backend จะเตือน)
- ถ้าบันทึกลงฐานข้อมูลไม่ได้ จอผู้เล่นยังได้รับข้อมูลตามปกติ ข้อผิดพลาดอยู่ใน log
- ชื่อที่เลือกอยู่ไม่ถูกเก็บข้าม restart ต้องเลือกใหม่ (รอบที่บันทึกแล้วยังคงชื่อเดิม)

## สิ่งที่ game engine บน Pi ต้องทำ

ไม่มี LED แล้ว หน้าเว็บจึงเป็นสัญญาณเดียวของผู้เล่น **เริ่มรอบได้เมื่อ `rlgl/status/web-01` และ `rlgl/status/backend-01` เป็น `online` ทั้งคู่** ถ้าตัวใดเป็น `offline` ระหว่างเล่น ให้ยกเลิกรอบ

- `web-01` backend ประกาศแทน ตามว่ามีจอ `role=display` ต่ออยู่ไหม
- ถ้า backend ตายกะทันหัน broker ประกาศได้แค่ `backend-01` เป็น offline ส่วน `web-01` จะค้างค่าเดิม จึงต้องดูทั้งสองตัว
- `online` หมายถึง browser ต่ออยู่และยังส่ง ping **ไม่ได้ยืนยันว่าผู้เล่นมองเห็นจอ** และ browser อาจชะลอ timer ของแท็บที่ถูกซ่อน

## สถานะและข้อจำกัด

- เทสต์อัตโนมัติ: backend 162 ข้อ และส่วนคำนวณของหน้าเว็บ 17 ข้อ ผ่าน
- วิดีโอใน cloud (26 พ.ย.) ทดสอบกับ backend จริงและ**ตัวจำลอง Supabase Storage บนเครื่องนี้** (แปลง JPEG เป็น MP4, อัปโหลด, ไม่มีไฟล์ค้างใน laptop, ลิงก์ส่งต่อ, browser เล่นและเลื่อนตำแหน่งได้, cloud ใช้ไม่ได้แล้วตอบ 503) **ยังไม่ได้ลองกับ Supabase จริง**
- ไฟล์หน้าเว็บส่ง `Cache-Control: no-cache` ให้ browser เช็กของใหม่ทุกครั้ง (ถ้าเคยเปิดหน้าเว็บก่อน 26 พ.ย. ให้กด Ctrl+F5 หนึ่งครั้ง)
- หน้าเว็บ (26 พ.ย.) ทดสอบใน browser กับ backend จริงและตัวจำลองผ่าน `/dev/publish` ทั้งสองโหมด (ใส่ชื่อ, calibrate, ไฟเขียว/แดง, Sniper, ชนะ, แพ้, scoreboard, ประวัติ, วิดีโอ, หลุดแล้วต่อใหม่) **ยังไม่ได้ลองกับภาพสดจาก Pi จริง** และยังไม่ได้ทดสอบผ่าน broker
- timeline ในหน้าผลวาดจากข้อมูลที่หน้านั้นเห็นระหว่างรอบ ถ้าเปิดหน้าเว็บกลางรอบหรือ reload จะไม่มี timeline ของรอบนั้น
- ส่วนรับ MQTT, สรุปรอบ และวิดีโอ เคยทดสอบรวมกับ broker, InfluxDB และตัวจำลองบนเครื่องเดียว (25 พ.ย.)
- ส่วนฐานข้อมูลผู้เล่น (26 พ.ย.) ทดสอบกับ backend จริงโดยป้อนข้อความจากตัวจำลองผ่าน `/dev/publish` **ยังไม่ได้ทดสอบผ่าน broker และ InfluxDB**
- ยังไม่ได้ทดสอบกับ Pi จริง, ผ่าน Wi-Fi, หรือกับ browser จริง (ใช้โปรแกรมจำลองจอ)
- backend เก็บสถานะรอบไว้ในหน่วยความจำ ถ้า restart กลางรอบ จะไม่มีสรุปของรอบนั้น
- `red_acc_peak_max` ใช้เวลาที่รับข้อความ:
  - ตอนเปลี่ยนจาก `RED_LIGHT` เป็น `GREEN_LIGHT` **ไม่นับค่าข้อมือที่มาถึงใน 200 ms สุดท้าย** (`GREEN_EDGE_MARGIN_S` ใน `tracker.py`) เพราะค่าตอนเริ่มเดินอาจมาถึงก่อน state ใหม่ การขยับจริงในช่วงนั้นก็จะไม่ถูกนับด้วย ถ้า Wi-Fi จริงสลับลำดับเกิน 200 ms ต้องปรับค่านี้
  - ถ้ารอบจบระหว่างไฟแดง (`GAME_OVER` หรือแตะเส้นชัย) นับทุกค่า เพื่อไม่ให้การขยับที่ทำให้แพ้หายไป
  - ค่าที่วัดช่วงผ่อนผันแต่มาถึงช้ายังอาจถูกนับรวม
  - การตัดนี้มีผลเฉพาะสรุปรอบ **ไม่เกี่ยวกับการตัดสินของ game engine** และไม่ได้ตัด `red_motion_max` ของกล้อง
- WebSocket และ API ไม่มีระบบล็อกอิน ใครที่อยู่ในเครือข่ายเดียวกันอ่านประวัติและเปลี่ยนชื่อผู้เล่นได้ ใช้ในเครือข่ายของกลุ่มเท่านั้น
- ยังไม่มีคำสั่งแก้ชื่อหรือลบรอบ ถ้าจะเริ่มข้อมูลใหม่ให้ปิด backend แล้วย้ายโฟลเดอร์ `data/` ออก
