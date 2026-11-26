# Game engine (รันบน laptop)

ตัวคุมเกมและตัดสินแพ้ชนะ ต่อ broker บน Pi แล้ว:

- ส่ง `rlgl/game/state`, `rlgl/game/result` และ `rlgl/game/calibration`
- ฟังข้อมือ (`rlgl/wrist/*`), กล้องบน Pi (`rlgl/gateway/vision`), เส้นชัย (`rlgl/gateway/finish`), Sniper (`rlgl/sniper/aim`) และโหมดที่เลือกบนหน้าเว็บ (`rlgl/game/mode`)

## เปิด

ดับเบิลคลิก `laptop\engine\run.cmd` หรือรันในโฟลเดอร์ `laptop`:

```powershell
python -m engine
```

engine อ่าน IP ของ broker จาก `RLGL_MQTT_HOST` ใน `infra/.env` (ค่าเดียวกับ backend) ถ้าจะระบุเองให้ใช้ `--host 172.20.10.2`

**ลำดับการเปิด:** broker บน Pi → `pi_client.py` บน Pi → ESP → `web\run.cmd` (backend + หน้าเว็บ) → engine

## ใช้งาน

| วิธีเริ่มรอบ | |
|---|---|
| กด Enter ในหน้าต่าง engine | |
| ปุ่ม START บน joystick | ESP2 ส่ง `rlgl/station/button` `{"pressed": 1}` |
| ส่ง `rlgl/cmd/game` `{"cmd": "start"}` | |

คำสั่งอื่นในหน้าต่าง engine:

| คำสั่ง | ทำอะไร |
|---|---|
| `a` | ยกเลิกรอบ |
| `t` | จำลองแตะเส้นชัย (ใช้ตอนยังไม่มี touch sensor) |
| `s` | ดูอุปกรณ์ที่ online และสิ่งที่ยังขาด |
| `q` หรือ Ctrl+C | ออก |

ใส่ชื่อผู้เล่นและเลือกโหมดบนหน้าเว็บก่อนเริ่มรอบ

### อุปกรณ์ที่ต้อง online ก่อนเริ่มรอบ

| อุปกรณ์ | ถ้าหลุดระหว่างเล่น |
|---|---|
| `backend-01` | ยกเลิกรอบ (`disconnect`) |
| `web-01` (มีหน้าเว็บเปิดอยู่) | ยกเลิกรอบ (`disconnect`) |
| `wrist-01` | ยกเลิกรอบ (`disconnect`) |
| Pi (`rlgl/status/pi` หรือ `gateway-01`) | ยกเลิกรอบ (`camera_lost`) |
| broker | engine หลุดจาก broker ระหว่างเล่น ก็ยกเลิกรอบ (`disconnect`) |

ถ้ามีอุปกรณ์ไม่ครบ engine จะไม่เริ่มรอบ และพิมพ์บอกว่าขาดตัวไหน
ถ้าต้องการทดสอบตอนอุปกรณ์ยังไม่ครบ ให้ใช้ `--skip-checks`

## กติกา

### โหมด RLGL

```
CALIBRATE (5 วิ) → GREEN_LIGHT (สุ่ม 3-6 วิ) → TRANSITION (1.5 วิ, ยังไม่จับ) → RED_LIGHT (สุ่ม 3-5 วิ) → GREEN_LIGHT …
```

| เหตุการณ์ | ผล | `reason` |
|---|---|---|
| ไฟแดง: Pi ส่ง `rlgl/gateway/vision` ที่มี `"event": "moved"` | แพ้ | `camera` |
| ไฟแดง: ข้อมือ `acc_peak` > 0.3 g หรือ `gyro_rms` > 50 deg/s ติดกัน 2 ค่า (ไม่นับ 0.3 วินาทีแรกของไฟแดง) | แพ้ | `imu` |
| ไฟแดง: กล้องและข้อมือจับได้พร้อมกัน | แพ้ | `both` |
| แตะเส้นชัยตอนไฟเขียวหรือตอนกล้องหัน | ชนะ | `touch` |
| แตะเส้นชัยตอนไฟแดง | แพ้ | `finish_during_red` |
| ครบ 60 วินาทีนับจากไฟเขียวแรก | แพ้ | `timeout` |

ชีพจรขณะพัก (`bpm_rest`) = ค่าเฉลี่ยชีพจรช่วง calibrate

### โหมด Sniper

```
CALIBRATE → SNIPER (60 วิ, 3 ชีวิต)
```

- **โดนยิง:** Pi ส่ง `rlgl/sniper/aim` ที่ `lock_s` ถึง `lock_limit_s` นับ 1 ครั้ง (หรือส่ง `"event": "hit"`) ต้องหลุดกรอบก่อนจึงนับครั้งถัดไป
- **ชนะ:** แตะปุ่ม
- **แพ้:** โดนยิงครบ 3 ครั้ง หรือหมดเวลา
- **`dodges`:** นับจากข้อมือ `acc_peak` ≥ 0.6 g แต่ละครั้งห่างกันอย่างน้อย 0.5 วินาที ไม่ทำให้แพ้

### หลังจบรอบ

- `WIN` / `GAME_OVER` ค้างไว้จนรอบถัดไปเริ่ม หน้าเว็บจะแสดงผลจนกว่าจะกด PLAY AGAIN
- ยกเลิกรอบ: ส่งผล `ABORTED` แล้ว state กลับ `IDLE`

### ปรับค่า

ปรับได้ด้วย option (ดูทั้งหมดด้วย `python -m engine --help`):

```powershell
python -m engine --green 3 6 --red 3 5 --limit 60 --imu-acc 0.3 --imu-gyro 50
```

เกณฑ์ของข้อมือ (0.3 g, 50 deg/s) เป็นค่าเริ่มต้น **ยังไม่ได้ปรับกับ ESP จริง** ให้ดู `acc_peak` และ `gyro_rms` ตอนยืนนิ่งใน Grafana แล้วตั้งให้สูงกว่าค่านั้นพอสมควร

## ข้อตกลงที่ต้องรู้

- `round_id` เป็น**ตัวเลข** (เวลา epoch เป็นวินาที) เพราะ `pi_client.py` จัดรูปแบบด้วย `:03d` ค่านี้ไม่ซ้ำแม้ปิดเปิด engine ใหม่ backend เก็บเป็นข้อความ
- ส่ง `rlgl/game/result` **ก่อน** state สุดท้ายทุกครั้ง เพราะ `pi_client.py` อ่านผลล่าสุดตอนได้ state `GAME_OVER` / `WIN`
- ผลมี `cause`, `part`, `score` ซ้ำกับ `reason`, `caught_part`, `caught_score_ratio` เพราะ `pi_client.py` อ่านชื่อเหล่านั้น
- engine ใช้ `device_id` = `game-01` และตั้ง LWT ที่ `rlgl/status/game-01`
- **ถ้า engine ถูกปิดแบบกะทันหัน** (เช่นปิดหน้าต่าง): state สุดท้ายจะค้างใน broker และหน้าเว็บจะค้างที่หน้าเดิม เปิด engine ใหม่แล้ว engine จะส่ง `IDLE` ให้เอง

## ทดสอบ

```powershell
cd laptop\engine
```

```powershell
python -m pytest
```

### ผลทดสอบ (26 พ.ย. 2026)

**unit test:** 33 ข้อผ่าน

**ทดสอบทั้งระบบ** บน broker ทดสอบบน laptop กับ backend ชุดทดสอบ (port 8001) โดยใช้อุปกรณ์ปลอม เล่นครบทุกแบบ:

- แพ้จากกล้อง
- แพ้จากข้อมือ
- ชนะ
- Pi หลุดระหว่างเล่น
- Sniper โดนยิงครบ 3 ครั้ง

ผลที่ได้:

- หน้าเว็บแสดงไฟเขียว/แดง หน้าผล และ timeline ถูกต้อง
- รอบบันทึกพร้อมชื่อผู้เล่น

**ยังไม่ได้ทดสอบ:** เล่นจริงกับ Pi, ESP และ touch sensor
