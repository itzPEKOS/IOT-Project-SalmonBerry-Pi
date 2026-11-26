# RLGL Web Dashboard

เว็บ static (HTML + JS ล้วน ไม่ต้อง build) ต่อ MQTT ผ่าน WebSocket และฝังกราฟจาก Grafana ด้วย iframe

## วิธีรัน

```bash
# ในโฟลเดอร์นี้ (บนโน้ตบุ๊ก)
python -m http.server 8000
```

* เปิด `http://localhost:8000/?demo=1` เพื่อลองหน้าตาโดยไม่ต้องมี broker (จำลองเกมในเบราว์เซอร์)
* เปิด `http://localhost:8000/` เพื่อใช้งานจริง (ต่อ broker ตาม `config.js`)

## สิ่งที่ต้องตั้งค่าก่อนใช้งานจริง

1. **เปิด WebSocket ที่ Mosquitto บน Pi** (เบราว์เซอร์ต่อ MQTT ตรงๆ ไม่ได้ ต้องผ่าน WebSocket) เพิ่มใน `/etc/mosquitto/conf.d/rlgl.conf` แล้ว `sudo systemctl restart mosquitto` (ถ้ามี firewall บน Pi ให้เปิด port 9001 ด้วย):
   ```
   listener 1883 0.0.0.0
   protocol mqtt
   allow_anonymous true

   listener 9001 0.0.0.0
   protocol websockets
   ```
2. แก้ `config.js`: `PI_HOST` (IP ของ Pi ใช้ทั้ง broker, `PI_VIDEO_URL` และ `PI_CAPTURES_URL`), ที่อยู่ Grafana, `UID` / `SLUG` ของ dashboard และ `panelId` ของแต่ละ panel
3. ตั้ง `grafana.ini` ตาม spec หัวข้อ 3 (`allow_embedding`, anonymous viewer, `min_refresh_interval = 1s`)

## Component ↔ แหล่งข้อมูล

| Component | แหล่งข้อมูล |
| :--- | :--- |
| Game State Banner, Countdown, Game Over Reason, Grace Period | `rlgl/game/state` |
| Live Video + กรอบ AI | MJPEG จาก Pi (HTTP) **Pi ต้องวาดกรอบลงเฟรมเอง** |
| FPS, การขยับ | `rlgl/pi/motion` |
| Distance Progress Bar | `rlgl/pi/distance` |
| BPM, Gyro Meter, กราฟย้อนหลัง, Leaderboard | Grafana iframe (จาก InfluxDB) |
| Round Selector | `rlgl/game/rounds` |
| AI Commentator | `rlgl/ai/commentary` |
| ปุ่ม Start/Reset/Calibrate, Servo Mode | publish `rlgl/cmd/game`, `rlgl/cmd/servo_mode` |
| Device Status, Disconnect Alert | `rlgl/status/esp1\|esp2\|pi` (LWT) + สถานะ WebSocket |
| Calibration Result | `rlgl/game/calibration` |
| Evaluation Panel | `rlgl/pi/eval` |
| MQTT Topic Monitor | subscribe `rlgl/#` |

## MQTT Topics ที่เว็บใช้ (ข้อเสนอ ปรับได้ที่ `config.js`)

ทุก topic อยู่ใต้ `rlgl/` ตามข้อตกลงของทีม (ไม่ต้องมี bridge แล้ว แต่ยังใช้ prefix เดิมเพื่อให้ย้าย broker หรือกรอง topic ได้ง่าย)

| Topic | ทิศทาง | Retained | Payload |
| :--- | :--- | :--- | :--- |
| `rlgl/game/state` | Engine → เว็บ | ใช่ | `{"state":"RED_LIGHT","remaining_s":3.0,"round_id":5,"mode":"auto","reason":null,"part":null}` |
| `rlgl/game/calibration` | Engine → เว็บ | ไม่ | `{"bpm_baseline":74,"gyro_baseline":0.06}` |
| `rlgl/game/rounds` | Engine → เว็บ | ใช่ | `[{"id":1,"start_ms":1764000000000,"end_ms":1764000030000,"result":"ชนะ"}]` |
| `rlgl/pi/distance` | Pi → เว็บ | ไม่ | `{"cm":182.4}` |
| `rlgl/pi/motion` | Pi → Engine/เว็บ | ไม่ | `{"moved":true,"part":"left_arm","score":0.08,"fps":14.1}` |
| `rlgl/pi/eval` | Pi → เว็บ | ใช่ | `{"fps_avg":14.2,"accuracy":0.925,"n":40,"tp":18,"fn":2,"tn":19,"fp":1}` |
| `rlgl/status/esp1`, `esp2`, `pi` | อุปกรณ์ (LWT) → เว็บ | ใช่ | `online` / `offline` (ข้อความธรรมดา) |
| `rlgl/ai/commentary` | Engine → เว็บ | ไม่ | `{"text":"...","source":"gemini"}` หรือ `"source":"fallback"` |
| `rlgl/cmd/game` | เว็บ → Engine | ไม่ | `{"cmd":"start"}` / `reset` / `calibrate` |
| `rlgl/cmd/servo_mode` | เว็บ → Engine | ไม่ | `{"mode":"auto"}` หรือ `"joystick"` |

* `state` มีค่าได้: `IDLE`, `GREEN_LIGHT`, `TRANSITION`, `RED_LIGHT`, `WIN`, `GAME_OVER`, `PAUSED`
* `reason` (ตอน `GAME_OVER`): `gyro`, `camera`, `touch_red`
* นับถอยหลังบนเว็บคำนวณเองจาก `remaining_s` ที่ได้รับล่าสุด Engine ส่งตอนเปลี่ยน state ครั้งเดียวก็พอ
* LWT: ให้ ESP/Pi ตั้ง Last Will เป็น `offline` (retained) ที่ topic ของตัวเอง และ publish `online` (retained) ตอนต่อสำเร็จ

## ไฟล์

* `index.html`, `style.css`, `app.js`, `config.js` ตัวเว็บ
* `mqtt.min.js` ไลบรารี mqtt.js (เก็บไว้ในเครื่อง เผื่อวันเดโมไม่มีเน็ต)
