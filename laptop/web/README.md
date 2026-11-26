# เว็บเกม: backend และ frontend

```
web/
├── backend/    FastAPI (เสร็จขั้นแรกแล้ว)
├── frontend/   หน้าเว็บ (รอ design) วาง index.html ที่นี่
├── tests/      เทสต์ของ backend
└── clips/      วิดีโอที่รับจาก Pi (สร้างเอง, ไม่ commit)
```

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

ส่งข้อมูลจำลองพร้อมคลิปทดสอบ (ต้องมี ffmpeg ใน PATH) **ไม่ต้องใส่ `--fake-backend`**

```powershell
python ../sim/simulate.py --rounds 3 --backend http://127.0.0.1:8000
```

รันเทสต์:

```powershell
python -m pytest
```

## ตั้งค่า (environment variable หรือใส่ใน `../infra/.env`)

| ค่า | ค่าเริ่มต้น | ใช้ทำอะไร |
|---|---|---|
| `RLGL_MQTT_HOST`, `RLGL_MQTT_PORT` | `127.0.0.1`, `1883` | ที่อยู่ broker (ตอนรวมระบบใส่ IP ของ Pi) ใส่ค่าว่างเพื่อไม่ต่อ MQTT |
| `MQTT_USER`, `MQTT_PASSWORD` | อ่านจาก `.env` | บัญชี MQTT |
| `RLGL_UPLOAD_TOKEN` | ไม่ตั้ง | ถ้าตั้ง การอัปโหลดต้องส่ง header `X-Upload-Token` |
| `RLGL_MAX_UPLOAD_MB` | `300` | ขนาดวิดีโอสูงสุด |
| `RLGL_PUBLIC_BASE_URL` | ตามที่อยู่ที่ Pi เรียกเข้ามา | ใช้เมื่อลิงก์วิดีโอต้องเป็นที่อยู่อื่น |
| `RLGL_CORS_ORIGINS` | ไม่ตั้ง | ที่อยู่ dev server ของ frontend คั่นด้วย `,` เช่น `http://localhost:5173` |
| `RLGL_DEV` | ไม่ตั้ง | `1` = เปิด `POST /dev/publish` (เรียกได้จากเครื่องนี้เท่านั้น) |

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
| `rlgl/game/summary` | `round_id`, `bpm_avg`, `bpm_max`, `red_motion_max`, `red_acc_peak_max`, `red_light_count` | สรุปรอบ (field ที่ไม่มีข้อมูลจะไม่ถูกส่งมา) |
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
| `GET /clips/<round_id>.mp4` | ไฟล์วิดีโอ รองรับการเลื่อนตำแหน่ง |
| `POST /clips` | ใช้โดย Pi (ดูข้อตกลง MQTT) |

ถ้าวางไฟล์ใน `frontend/` โดยมี `index.html` backend จะเปิดให้ที่ http://localhost:8000/ (ต้อง restart backend ครั้งแรก)

## สิ่งที่ game engine บน Pi ต้องทำ

ไม่มี LED แล้ว หน้าเว็บจึงเป็นสัญญาณเดียวของผู้เล่น **เริ่มรอบได้เมื่อ `rlgl/status/web-01` และ `rlgl/status/backend-01` เป็น `online` ทั้งคู่** ถ้าตัวใดเป็น `offline` ระหว่างเล่น ให้ยกเลิกรอบ

- `web-01` backend ประกาศแทน ตามว่ามีจอ `role=display` ต่ออยู่ไหม
- ถ้า backend ตายกะทันหัน broker ประกาศได้แค่ `backend-01` เป็น offline ส่วน `web-01` จะค้างค่าเดิม จึงต้องดูทั้งสองตัว
- `online` หมายถึง browser ต่ออยู่และยังส่ง ping **ไม่ได้ยืนยันว่าผู้เล่นมองเห็นจอ** และ browser อาจชะลอ timer ของแท็บที่ถูกซ่อน

## สถานะและข้อจำกัด

- เทสต์อัตโนมัติ 58 ข้อผ่าน และทดสอบรวมกับ broker, InfluxDB และตัวจำลองบนเครื่องเดียว
- ยังไม่ได้ทดสอบกับ Pi จริง, ผ่าน Wi-Fi, หรือกับ browser จริง (ใช้โปรแกรมจำลองจอ)
- backend เก็บสถานะรอบไว้ในหน่วยความจำ ถ้า restart กลางรอบ จะไม่มีสรุปของรอบนั้น
- `red_acc_peak_max` ใช้เวลาที่รับข้อความ:
  - ตอนเปลี่ยนจาก `RED_LIGHT` เป็น `GREEN_LIGHT` **ไม่นับค่าข้อมือที่มาถึงใน 200 ms สุดท้าย** (`GREEN_EDGE_MARGIN_S` ใน `tracker.py`) เพราะค่าตอนเริ่มเดินอาจมาถึงก่อน state ใหม่ การขยับจริงในช่วงนั้นก็จะไม่ถูกนับด้วย ถ้า Wi-Fi จริงสลับลำดับเกิน 200 ms ต้องปรับค่านี้
  - ถ้ารอบจบระหว่างไฟแดง (`GAME_OVER` หรือแตะเส้นชัย) นับทุกค่า เพื่อไม่ให้การขยับที่ทำให้แพ้หายไป
  - ค่าที่วัดช่วงผ่อนผันแต่มาถึงช้ายังอาจถูกนับรวม
  - การตัดนี้มีผลเฉพาะสรุปรอบ **ไม่เกี่ยวกับการตัดสินของ game engine** และไม่ได้ตัด `red_motion_max` ของกล้อง
- WebSocket และ API ไม่มีระบบล็อกอิน ใช้ในเครือข่ายของกลุ่มเท่านั้น
