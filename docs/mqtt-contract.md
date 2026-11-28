# ข้อตกลง MQTT (ทุกคนในทีมใช้ชื่อ topic และ field ตามนี้)

## กติการ่วม

- payload เป็น JSON object แบบชั้นเดียว
- ทุกข้อความมี `device_id` (string), `seq` (ตัวเลขเพิ่มทีละ 1) และ `ts` (epoch มิลลิวินาที ถ้า ESP32 ยังไม่ sync เวลา ให้ใส่ `millis()` ได้ เพราะ dashboard ใช้เวลาตอนรับข้อความ)
- ข้อความจาก `game`, `uploader`, `backend` มี `round_id` (ใช้ได้เฉพาะ `A-Z a-z 0-9 _ -` และต้องไม่ซ้ำกันในแต่ละรอบ)
- ค่าที่เป็นจริง/เท็จ ให้ส่งเป็น `1` / `0`
- ชื่อ state: `IDLE`, `CALIBRATE`, `GREEN_LIGHT`, `TRANSITION`, `RED_LIGHT`, `GAME_OVER`, `WIN`

## Topic และ field

| Topic | ผู้ส่ง | QoS / retain | field (นอกจากกติการ่วม) |
|---|---|---|---|
| `rlgl/wrist/imu` | ESP32 #1 | 0 | `acc_rms` (g), `acc_peak` (g), `gyro_rms` (deg/s) |
| `rlgl/wrist/heartrate` | ESP32 #1 | 0 | `bpm` (ส่งเฉพาะค่า 45-180) |
| `rlgl/station/camera` | ESP32 #2 | 1 / retain | `pan`, `tilt` (องศา), `facing` = `away`, `turning`, `player` |
| `rlgl/station/button` | ESP32 #2 | 1 | `pressed` = 1 |
| `rlgl/gateway/finish` | finish-01 (ESP32) | 0* | `touched` = 1, `round_id`, `state` (state ตอนแตะ), `elapsed_s` (วินาทีตั้งแต่ไฟเขียวแรกของรอบ, `null` = ยังไม่เริ่ม), `first` (1 = ครั้งแรกของรอบ), `press_count` |
| `rlgl/gateway/distance` | finish-01 (ESP32) | 0 | `distance_cm` (ไม่มี field นี้เมื่อวัดไม่ได้), `valid` (1/0), `round_id` ส่ง 10 Hz ตอนเล่น, 1 Hz ตอนว่าง |
| `rlgl/gateway/vision` | vision | 0 | `motion_score`, `confidence` (0-1), `fps`, `inference_ms` |
| `rlgl/gateway/replay` | vision | 1 | ส่งหลังอัปโหลดคลิปของรอบสำเร็จ: `round_id`, `video_url`, `moved_at` (วินาทีในคลิปที่กล้องจับได้ว่าขยับ, `null` = ไม่ได้แพ้เพราะกล้อง), `ended_at` (วินาทีในคลิปที่เกมจบ), `duration`, `result`, `reason`, `part`, `joint`, `motion_score`, `clip_started_at`, `moved_ts` (epoch ms) หน้าเว็บใช้ `video.currentTime = moved_at` กระโดดไปจังหวะที่ขยับได้ |
| `rlgl/game/state` | game | 1 / retain | `round_id`, `state`, `remaining_s` ส่งเมื่อเปลี่ยน และทุก 1 วินาทีระหว่างเล่น |
| `rlgl/game/result` | game | 1 | `round_id`, `result`, `reason`, `started_at`, `ended_at` (epoch ms), `duration_s` |
| `rlgl/game/media` | backend | 1 | `round_id`, `status` = `ready`, `video_url` |
| `rlgl/game/summary` | backend | 1 | `round_id`, `bpm_avg`, `bpm_max`, `red_motion_max`, `red_acc_peak_max`, `red_light_count` (ค่าที่ไม่มีข้อมูลจะไม่ถูกส่ง; `red_acc_peak_max` ไม่นับ 200 ms สุดท้ายก่อนเปลี่ยนเป็นไฟเขียว) |
| `rlgl/status/<device_id>` | ทุกอุปกรณ์ | 1 / retain, ตั้ง LWT | `status` = `online`, `offline` |

`device_id` ของแต่ละตัว: `wrist-01`, `station-01`, `finish-01`, `gateway-01`, `backend-01`, `web-01`

\* ESP32 ใช้ PubSubClient ซึ่ง publish ได้แค่ QoS 0 ถ้า MQTT หลุดตอนแตะ `finish-01` จะเก็บข้อความไว้แล้วส่งทันทีที่ต่อได้

**ไม่มี LED:** หน้าเว็บเกมเป็นจอสถานะของผู้เล่น หน้าเว็บต่อกับ backend ผ่าน WebSocket (ไม่ต่อ MQTT เอง) และ **backend เป็นคน publish `rlgl/status/web-01`** ตามว่ามีจอผู้เล่นต่ออยู่ไหม
backend ตั้ง LWT ให้ `backend-01` เท่านั้น game engine จึงต้องให้ **`web-01` และ `backend-01` เป็น `online` ทั้งคู่** จึงเริ่มรอบได้ ถ้าตัวใด `offline` ระหว่างเล่นให้ยกเลิกรอบ (`reason` = `disconnect`)

## `result` และ `reason`

| `result` | `reason` ที่ใช้ได้ |
|---|---|
| `WIN` | `touch` |
| `GAME_OVER` | `camera`, `imu`, `both`, `timeout`, `finish_during_red` |
| `ABORTED` (ยกเลิกรอบ แล้ว state กลับ `IDLE`) | `disconnect`, `camera_lost` |

## การส่งวิดีโอ (HTTP ไม่ใช่ MQTT)

`POST http://<IP ของ laptop>:8000/clips` แบบ `multipart/form-data`

| ช่อง | ค่า |
|---|---|
| `round_id` | ตรงกับรอบใน `rlgl/game/state` |
| `video` | ไฟล์ MP4 แบบ H.264, `yuv420p` |
| `meta` (ไม่บังคับ) | ไฟล์ JSON ของคลิปจาก Pi: `moved_at`, `ended_at`, `clip_started_at` และ `frames` (เวลา, state, motion score รายเฟรม) backend ตอนนี้ยังไม่ได้เก็บช่องนี้ |

ถ้า backend ตั้ง `RLGL_UPLOAD_TOKEN` ต้องส่ง header `X-Upload-Token` ด้วย
ตอบ `200` พร้อม `{"url": "..."}` เมื่อบันทึกสำเร็จ ถ้าได้คำตอบอื่นให้ Pi เก็บไฟล์ไว้และลองส่งใหม่

## ตัวอย่าง

```json
{"device_id":"gateway-01","seq":88,"ts":1795000000123,"round_id":"r012","state":"RED_LIGHT","remaining_s":41}
```

```json
{"device_id":"gateway-01","seq":90,"ts":1795000004500,"round_id":"r012","result":"GAME_OVER","reason":"camera","started_at":1794999985000,"ended_at":1795000004500,"duration_s":19.5}
```

## ภาพสดจากกล้อง (HTTP บน Pi)

`pi_client.py` เปิด HTTP ที่ port 8000 ของ Pi (ถ้า port ไม่ว่างจะเลื่อนไปตัวถัดไปและพิมพ์บอก)

| ที่อยู่ | ได้อะไร |
|---|---|
| `http://<IP ของ Pi>:8000/stream` | MJPEG ต่อเนื่อง วาดโครงร่างและจุดที่ขยับแล้ว ใช้ `<img src="...">` ได้เลย (เช่นตอน `CALIBRATE` ให้ผู้เล่นเห็นว่ากล้องเห็นตัวไหม) |
| `http://<IP ของ Pi>:8000/snapshot.jpg` | ภาพล่าสุดภาพเดียว |
| `http://<IP ของ Pi>:8000/status` | JSON: `state`, `player_seen`, `visible_joints`, `calib_frames`, `fps` |

ระหว่าง `CALIBRATE` Pi ส่ง `rlgl/gateway/vision` ที่มี `event` = `heartbeat` ทุก 0.5 วินาที พร้อม `player_seen` (1/0), `visible_joints` (เห็นกี่จุดจาก 17) และ `calib_frames`
