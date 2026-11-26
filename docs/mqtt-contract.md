# ข้อตกลง MQTT (ทุกคนในทีมใช้ชื่อ topic และ field ตามนี้)

## กติการ่วม

- payload เป็น JSON object แบบชั้นเดียว
- ทุกข้อความมี `device_id` (string), `seq` (ตัวเลขเพิ่มทีละ 1) และ `ts` (epoch มิลลิวินาที ถ้า ESP32 ยังไม่ sync เวลา ให้ใส่ `millis()` ได้ เพราะ dashboard ใช้เวลาตอนรับข้อความ)
- ข้อความจาก `game`, `uploader`, `backend` มี `round_id` (ใช้ได้เฉพาะ `A-Z a-z 0-9 _ -` และต้องไม่ซ้ำกันในแต่ละรอบ)
  game engine (`laptop/engine`) ส่ง `round_id` เป็น**ตัวเลข** (epoch วินาที) เพราะ `pi_client.py` จัดรูปแบบด้วย `:03d` backend แปลงเป็นข้อความเอง
- ค่าที่เป็นจริง/เท็จ ให้ส่งเป็น `1` / `0`
- ชื่อ state: `IDLE`, `CALIBRATE`, `GREEN_LIGHT`, `TRANSITION`, `RED_LIGHT`, `GAME_OVER`, `WIN`

## Topic และ field

| Topic | ผู้ส่ง | QoS / retain | field (นอกจากกติการ่วม) |
|---|---|---|---|
| `rlgl/wrist/imu` | ESP32 #1 | 0 | `acc_rms` (g), `acc_peak` (g), `gyro_rms` (deg/s) |
| `rlgl/wrist/heartrate` | ESP32 #1 | 0 | `bpm` (ส่งเฉพาะค่า 45-180) |
| `rlgl/station/camera` | ESP32 #2 | 1 / retain | `pan`, `tilt` (องศา), `facing` = `away`, `turning`, `player` |
| `rlgl/station/button` | ESP32 #2 | 1 | `pressed` = 1 |
| `rlgl/gateway/finish` | game | 1 | `touched` = 1, `round_id` |
| `rlgl/gateway/distance` | game | 0 | `distance_cm` |
| `rlgl/gateway/vision` | vision | 0 | `motion_score`, `confidence` (0-1), `fps`, `inference_ms` |
| `rlgl/game/state` | game | 1 / retain | `round_id`, `state`, `remaining_s` ส่งเมื่อเปลี่ยน และทุก 1 วินาทีระหว่างเล่น |
| `rlgl/game/result` | game | 1 | `round_id`, `result`, `reason`, `started_at`, `ended_at` (epoch ms), `duration_s` |
| `rlgl/game/media` | backend | 1 | `round_id`, `status` = `ready`, `video_url` |
| `rlgl/game/summary` | backend | 1 | `round_id`, `bpm_avg`, `bpm_max`, `red_motion_max`, `red_acc_peak_max`, `red_light_count`, `distance_cm` (ค่าสุดท้ายตอนเล่น), `finish_state` (ไฟตอนแตะเส้นชัย), `closest_call` (ค่าที่ไม่มีข้อมูลจะไม่ถูกส่ง; `red_acc_peak_max` ไม่นับ 200 ms สุดท้ายก่อนเปลี่ยนเป็นไฟเขียว) |
| `rlgl/status/<device_id>` | ทุกอุปกรณ์ | 1 / retain, ตั้ง LWT | `status` = `online`, `offline` |

`device_id` ของแต่ละตัว: `wrist-01`, `station-01`, `gateway-01`, `backend-01`, `web-01`, `game-01` (game engine บน laptop)
ตอนนี้ `pi_client.py` รายงานสถานะที่ `rlgl/status/pi` game engine รับทั้ง `pi` และ `gateway-01`

**เริ่มรอบ:** ESP2 ส่ง `rlgl/station/button` `{"pressed": 1}` (ปุ่ม START บน joystick) หรือส่ง `rlgl/cmd/game` `{"cmd": "start"}` (`"abort"` = ยกเลิกรอบ) game engine เริ่มรอบเมื่อ state เป็น `IDLE`, `WIN` หรือ `GAME_OVER`
**หลังจบรอบ:** state `WIN` / `GAME_OVER` ค้างไว้จนรอบถัดไปเริ่ม (หน้าเว็บแสดงผลจนกว่าจะกด PLAY AGAIN) ส่วน `ABORTED` กลับ `IDLE` ทันที
**ลำดับ:** game engine ส่ง `rlgl/game/result` ก่อน state สุดท้ายเสมอ และใส่ `cause`, `part`, `score` (ชื่อที่ `pi_client.py` อ่าน) ซ้ำกับ `reason`, `caught_part`, `caught_score_ratio`

**ไม่มี LED:** หน้าเว็บเกมเป็นจอสถานะของผู้เล่น หน้าเว็บต่อกับ backend ผ่าน WebSocket (ไม่ต่อ MQTT เอง) และ **backend เป็นคน publish `rlgl/status/web-01`** ตามว่ามีจอผู้เล่นต่ออยู่ไหม
backend ตั้ง LWT ให้ `backend-01` เท่านั้น game engine จึงต้องให้ **`web-01` และ `backend-01` เป็น `online` ทั้งคู่** จึงเริ่มรอบได้ ถ้าตัวใด `offline` ระหว่างเล่นให้ยกเลิกรอบ (`reason` = `disconnect`)

## field เพิ่มเติมที่ design หน้าเว็บต้องใช้ (ข้อเสนอ: คนทำ Pi ยังไม่ได้ตกลง)

ไม่บังคับ backend รับและเก็บเมื่อมี ถ้าไม่ส่ง หน้าเว็บจะไม่มีข้อมูลส่วนนั้น

| Topic | field | ความหมาย |
|---|---|---|
| `rlgl/gateway/vision` | `score_ratio` | คะแนนการขยับหารด้วยเกณฑ์ (1.0 = ถึงเกณฑ์) ใช้ทำ `closest_call` |
| `rlgl/game/result` | `caught_part` | ส่วนร่างกายที่ขยับ: `head`, `left_arm`, `right_arm`, `torso`, `left_leg`, `right_leg` |
| `rlgl/game/result` | `caught_red_s` | โดนจับหลังเริ่มไฟแดงกี่วินาที |
| `rlgl/game/result` | `caught_score_ratio` | คะแนนสูงสุดตอนโดนจับ เทียบเกณฑ์ |
| `rlgl/game/result` | `bpm_rest` | ชีพจรขณะพักจากช่วง calibrate |
| `rlgl/game/calibration` (topic ใหม่, game ส่ง) | `round_id`, `progress` (0-1), `player_detected` (1/0), `keypoints`, `camera_baseline_s`, `imu_baseline_s`, `baseline_s` | ส่งระหว่าง `CALIBRATE` ประมาณ 2 ครั้ง/วินาที ใช้ทำหน้า calibrate |

## โหมด Sniper (ข้อเสนอ: คนทำ Pi ยังไม่ได้ตกลง)

คนคุม joystick เล็งกล้อง pan-tilt ถ้าลำตัวผู้เล่นอยู่ในกรอบกลางภาพครบ 1 วินาที = โดนยิง 1 ครั้ง ผู้เล่นมี 3 ชีวิตและ 60 วินาทีเพื่อไปแตะปุ่ม (metal touch) ชนะเมื่อแตะปุ่ม คนคุมชนะเมื่อยิงครบ 3 ครั้งหรือหมดเวลา

| Topic | ผู้ส่ง | QoS / retain | field |
|---|---|---|---|
| `rlgl/game/mode` | backend | 1 / retain | `mode` = `rlgl` หรือ `sniper` (เลือกจากหน้าเว็บ) game engine อ่านค่านี้ก่อนเริ่มรอบ |
| `rlgl/game/state` | game | 1 / retain | เพิ่ม `mode` ทุกโหมด, state ระหว่างเล่น sniper คือ `SNIPER` และเพิ่ม `lives`, `hits`, `dodges` (ส่งทันทีเมื่อโดนยิง) |
| `rlgl/sniper/aim` | vision | 0 | `round_id`, `in_box` (1/0), `lock_s`, `lock_limit_s` (1.0), `offset_px`, `part` ส่งประมาณ 5-10 ครั้ง/วินาที |
| `rlgl/game/result` | game | 1 | เพิ่ม `mode`, `hits`, `dodges`; `reason` = `touch` (ชนะ), `hits` หรือ `timeout` (แพ้) |

- state ของ sniper: `IDLE` → `CALIBRATE` → `SNIPER` → `WIN` / `GAME_OVER`
- `dodges` = จำนวนครั้งที่ข้อมือขยับเร็ว (นับจาก IMU บน game engine)
- **ภาพสด:** Pi เปิด HTTP ส่ง MJPEG (เช่น `http://<IP ของ Pi>:8080/stream.mjpg`) ให้หน้าเว็บดึงเอง ไม่ผ่าน MQTT ควรวาดโครงร่างลงภาพบน Pi ก่อนส่ง ตั้งที่อยู่ที่ backend ด้วย `RLGL_STREAM_URL`
- ถ้า game engine ยังไม่ส่ง `mode` backend จะบันทึกรอบตามโหมดที่เลือกบนหน้าเว็บ

`round_id` ต้องไม่ซ้ำแม้ game engine ปิดเปิดใหม่ เพราะ backend เก็บผลของแต่ละ `round_id` ถาวรและรับเฉพาะผลแรก (เช่น ใส่วันเวลาใน `round_id`)

## `result` และ `reason`

| `result` | `reason` ที่ใช้ได้ |
|---|---|
| `WIN` | `touch` |
| `GAME_OVER` | `camera`, `imu`, `both`, `timeout`, `finish_during_red`, `hits` (sniper) |
| `ABORTED` (ยกเลิกรอบ แล้ว state กลับ `IDLE`) | `disconnect`, `camera_lost`, `operator` (คนคุมกดยกเลิก) |

## การส่งวิดีโอ (HTTP ไม่ใช่ MQTT)

**อัดทั้งรอบ:** Pi เขียน JPEG ทุกเฟรม (ชุดเดียวกับที่ส่งเป็นภาพสด MJPEG มีโครงร่างร่างกายวาดไว้ได้) ต่อกันลงไฟล์ในเครื่องตัวเอง ตั้งแต่เริ่มเล่นจนจบรอบ + 2 วินาที แล้วส่งไฟล์ทั้งก้อนครั้งเดียวหลังจบรอบ
laptop แปลงเป็น MP4 แล้วเก็บใน Supabase Storage **ไม่เก็บสำเนาไว้ใน laptop**

`POST http://<IP ของ laptop>:8000/clips` แบบ `multipart/form-data`

| ช่อง | ค่า |
|---|---|
| `round_id` | ตรงกับรอบใน `rlgl/game/state` |
| `video` | JPEG ต่อกันทั้งรอบ (ไฟล์ `.mjpeg` แต่ละเฟรมขึ้นต้นด้วย `FF D8`) หรือ MP4 แบบ H.264, `yuv420p` |
| `fps` | จำนวนเฟรมที่อัดได้ ÷ วินาทีที่อัดจริง (1-60, ไม่ส่ง = 15) ใช้กำหนดความยาวคลิปให้ตรงกับเวลาจริง |

- ถ้า backend ตั้ง `RLGL_UPLOAD_TOKEN` ต้องส่ง header `X-Upload-Token` ด้วย
- ตอบ `200` พร้อม `{"url": "..."}` **หลังเก็บขึ้น cloud สำเร็จแล้วเท่านั้น** ระหว่างนั้น laptop แปลงไฟล์และอัปโหลด อาจใช้เวลาหลายสิบวินาที ให้ตั้ง timeout อย่างน้อย 180 วินาที
- ถ้าได้ `503` (cloud หรือ internet ใช้ไม่ได้) หรือหลุดกลางทาง **ให้ Pi เก็บไฟล์ไว้และส่งใหม่ทีหลัง** ลบไฟล์บน Pi ได้เมื่อได้ `200` เท่านั้น
- `422` = ไฟล์เสียหรือค่าไม่ถูก ส่งซ้ำก็ไม่หาย (บันทึก log แล้วข้ามได้), `415` = ไม่ใช่ JPEG หรือ MP4, `413` = ใหญ่เกิน
- `video_url` ใน `rlgl/game/media` เป็นที่อยู่บน laptop (`/clips/<round_id>.mp4`) ไม่หมดอายุ เปิดแล้ว laptop จะส่งต่อไปยังลิงก์ชั่วคราวของ cloud

## ตัวอย่าง

```json
{"device_id":"gateway-01","seq":88,"ts":1795000000123,"round_id":"r012","state":"RED_LIGHT","remaining_s":41}
```

```json
{"device_id":"gateway-01","seq":90,"ts":1795000004500,"round_id":"r012","result":"GAME_OVER","reason":"camera","started_at":1794999985000,"ended_at":1795000004500,"duration_s":19.5}
```
