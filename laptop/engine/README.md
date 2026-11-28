# Game engine (รันบน laptop)

ตัวคุมเกมและตัดสินแพ้ชนะ ต่อ broker บน Pi แล้ว:

- ส่ง `rlgl/game/state`, `rlgl/game/result` และ `rlgl/game/calibration`
- ฟังข้อมือ (`rlgl/wrist/*`), กล้องและระยะบน Pi (`rlgl/gateway/vision`, `rlgl/gateway/distance`), เส้นชัย (`rlgl/gateway/finish`), Sniper (`rlgl/sniper/aim`) และโหมดที่เลือกบนหน้าเว็บ (`rlgl/game/mode`)

## เปิด

ดับเบิลคลิก `laptop\engine\run.cmd` หรือรันในโฟลเดอร์ `laptop`:

```powershell
python -m engine
```

engine อ่าน IP ของ broker จาก `RLGL_MQTT_HOST` ใน `infra/.env` (ค่าเดียวกับ backend) ถ้าจะระบุเองให้ใช้ `--host 172.20.10.2`

**ลำดับการเปิด:** broker บน Pi → สคริปต์กล้องบน Pi → ESP → `web\run.cmd` (backend + หน้าเว็บ) → engine

## หนึ่งรอบ

ใส่ชื่อผู้เล่นและเลือกโหมดบนหน้าเว็บก่อน แล้วใช้**ปุ่มเดียว** (ปุ่มบน joystick ของ ESP2 หรือ Enter ในหน้าต่าง engine):

```
IDLE ─กดครั้งที่ 1─→ CALIBRATE 5 วิ (ยืนนิ่ง + เช็ก sensor)
                       ├─ ผ่าน → READY ─กดครั้งที่ 2─→ COUNTDOWN (animation 2 วิ + นับ 3-2-1) → เล่น 30 วิ
                       └─ ไม่ผ่าน → CALIBRATE_FAILED ─กดอีกครั้ง─→ CALIBRATE ใหม่ (รอบเดิม)
```

- **ระหว่าง COUNTDOWN:** ขยับได้ ยังไม่จับ และแตะเส้นชัยก็ไม่นับ
- **ระหว่างเล่นเกม 1:** ปุ่มเดียวกันใช้เปลี่ยนไฟ (ดูหัวข้อ "เกม 1: เปลี่ยนไฟด้วยปุ่ม")
- **เวลาเล่น 30 วินาที:** เริ่มนับตอนไฟเขียวแรก (Sniper: ตอนเริ่ม `SNIPER`)

คำสั่งในหน้าต่าง engine:

| ปุ่ม | ทำอะไร |
|---|---|
| Enter | เหมือนกดปุ่ม joystick: calibrate (ครั้งแรก) / เริ่มเล่น (ตอน `READY`) / calibrate ใหม่ (ตอนเช็กไม่ผ่าน) / สลับไฟ (ระหว่างเล่นเกม 1) |
| `r` / `g` | เปลี่ยนเป็นไฟแดง (ผ่านช่วงเตือนก่อน) / กลับเป็นไฟเขียว ระบุสีได้ตรง ๆ |
| `t` | จำลองแตะเส้นชัย (ใช้ตอนยังไม่มี touch sensor) |
| `a` | ยกเลิกรอบ |
| `s` | ดูอุปกรณ์ที่ online, state ตอนนี้ และ sensor ที่บังคับ |
| `q` หรือ Ctrl+C | ออก |

### อุปกรณ์ที่ต้อง online ก่อนเริ่มรอบ

| อุปกรณ์ | ถ้าหลุดระหว่างรอบ (ตั้งแต่ calibrate จนจบ) |
|---|---|
| `backend-01` | ยกเลิกรอบ (`disconnect`) |
| `web-01` (มีหน้าเว็บเปิดอยู่) | ยกเลิกรอบ (`disconnect`) |
| `wrist-01` | ยกเลิกรอบ (`disconnect`) |
| Pi (`rlgl/status/pi` หรือ `gateway-01`) | ยกเลิกรอบ (`camera_lost`) |
| broker | engine หลุดจาก broker ก็ยกเลิกรอบ (`disconnect`) |

### เช็ก sensor ตอน calibrate

online อย่างเดียวไม่พอ ระหว่าง calibrate 5 วินาที sensor ต้องส่งข้อมูลจริง:

| sensor (`--sensors`) | อุปกรณ์ | ผ่านเมื่อ | บังคับตอนนี้ |
|---|---|---|---|
| `imu` | ESP ข้อมือ (MPU6050) | ได้ `rlgl/wrist/imu` อย่างน้อย 20 ค่า | ✓ |
| `heart` | ESP ข้อมือ (KY-039) | ได้ `bpm` 45-180 อย่างน้อย 1 ค่า | ✓ |
| `camera` | Pi (MediaPipe) | เห็นผู้เล่น (`player_seen` หรือ `valid`) ภายใน 2 วินาทีล่าสุด | ✓ |
| `distance` | ESP เส้นชัย `finish-01` (HC-SR04) | ได้ `rlgl/gateway/distance` อย่างน้อย 1 ค่า | ไม่บังคับ (ESP นี้เพิ่งเพิ่มเข้ามา) |
| `touch` | ESP เส้นชัย `finish-01` (แผ่นโลหะ) | `finish-01` online (touch ส่งข้อมูลเฉพาะตอนแตะ จึงเช็กล่วงหน้าได้แค่นี้) | ไม่บังคับ |
| `station` | ESP สถานี (joystick, servo) | `station-01` online | ESP สถานียังไม่พร้อม |

- **ESP เส้นชัย (`finish-01`):** ส่งทั้งระยะ (ultrasonic) และการแตะเส้นชัย engine ใช้ `rlgl/gateway/finish` ตัดสินชนะแทนการกด `t` ได้เลย
- **ไม่ผ่าน:** engine พิมพ์ว่าตัวไหนใช้ไม่ได้ และหน้าเว็บแสดง ✗ แดงที่ตัวนั้น แก้แล้วกด Enter เพื่อ calibrate ใหม่
- **sensor ที่ไม่บังคับ:** ยังแสดงบนหน้าเว็บ ถ้ามีข้อมูลขึ้น ✓ ถ้าไม่มีขึ้น NOT USED
- **เปลี่ยนรายการที่บังคับ:**

  | ต้องการ | คำสั่ง |
  |---|---|
  | บังคับ ESP เส้นชัยด้วย (เมื่อ `finish-01` พร้อม) | `python -m engine --sensors imu,heart,camera,distance,touch` |
  | บังคับครบทุกตัว (เมื่อ ESP สถานีพร้อมด้วย) | `python -m engine --sensors imu,heart,camera,distance,touch,station` |
  | ไม่บังคับชีพจร (ถ้า KY-039 วัดไม่ได้ในวันเดโม) | `python -m engine --sensors imu,camera` |
  | ไม่เช็กอะไรเลย (ไว้ทดสอบ) | `python -m engine --skip-checks` |

## ทดสอบกล้องอย่างเดียว

ใช้ตอน sensor อื่นยังไม่พร้อม: calibrate เช็กแค่กล้อง ตัดสินจากกล้องอย่างเดียว และไม่ต้องรอข้อมือ online

```powershell
.\run.cmd --camera-only
```

กด Enter เหมือนปุ่ม joystick: calibrate → เริ่มเล่น → สลับไฟ (ดูหัวข้อ "เกม 1: เปลี่ยนไฟด้วยปุ่ม")

## กติกา

### เกม 1 (RLGL): เปลี่ยนไฟด้วยปุ่ม

คนคุมกด**ปุ่ม**บน joystick (ESP2 ส่ง `rlgl/station/button` `{"pressed": 1}`) ไฟไม่สลับเองตามเวลา ส่วนการหมุน joystick ใช้เฉพาะเกม 2 (Sniper)

```
GREEN_LIGHT ─กด→ TRANSITION (เตือน 1.5 วิ, ยังไม่จับ) → RED_LIGHT ─กด→ GREEN_LIGHT …
```

- **ช่วงเตือน 1.5 วินาที:** ให้ผู้เล่นมีเวลาหยุด และให้ Pi เก็บท่าตั้งต้นตอนผู้เล่นนิ่งแล้ว กดระหว่างช่วงนี้ไม่มีผล ปรับความยาวด้วย `--transition`
- **ยังไม่มี ESP2:** กด Enter ในหน้าต่าง engine แทนได้ทุกอย่าง และใช้ `r` / `g` ระบุสีได้ตรง ๆ หรือส่ง `rlgl/cmd/game` `{"cmd": "red"}` / `{"cmd": "green"}`
- **ไฟสลับเองตามเวลาแบบเดิม:** เขียวสุ่ม 3-6 วิ, แดงสุ่ม 3-5 วิ ใช้ `--auto-lights` (ไว้ทดสอบตอนไม่มีคนคุม)

| เหตุการณ์ | ผล | `reason` |
|---|---|---|
| ไฟแดง: Pi ส่ง `rlgl/gateway/vision` ที่มี `"event": "moved"` | แพ้ | `camera` |
| ไฟแดง: ข้อมือ `acc_peak` > 0.3 g หรือ `gyro_rms` > 50 deg/s ติดกัน 2 ค่า (ไม่นับ 0.3 วินาทีแรกของไฟแดง) | แพ้ | `imu` |
| ไฟแดง: กล้องและข้อมือจับได้พร้อมกัน | แพ้ | `both` |
| แตะเส้นชัยตอนไฟเขียวหรือช่วงเตือน | ชนะ | `touch` |
| แตะเส้นชัยตอนไฟแดง | แพ้ | `finish_during_red` |
| ครบ 30 วินาทีนับจากไฟเขียวแรก | แพ้ | `timeout` |

ชีพจรขณะพัก (`bpm_rest`) = ค่าเฉลี่ยชีพจรช่วง calibrate

### โหมด Sniper

```
SNIPER (30 วิ, 3 ชีวิต)
```

- **โดนยิง:** Pi ส่ง `rlgl/sniper/aim` ที่ `lock_s` ถึง `lock_limit_s` นับ 1 ครั้ง (หรือส่ง `"event": "hit"`) ต้องหลุดกรอบก่อนจึงนับครั้งถัดไป
- **ชนะ:** แตะปุ่ม
- **แพ้:** โดนยิงครบ 3 ครั้ง หรือหมดเวลา
- **`dodges`:** นับจากข้อมือ `acc_peak` ≥ 0.6 g แต่ละครั้งห่างกันอย่างน้อย 0.5 วินาที ไม่ทำให้แพ้

### หลังจบรอบ

- `WIN` / `GAME_OVER` ค้างไว้จนรอบถัดไปเริ่ม หน้าเว็บจะแสดงผลจนกว่าจะกด PLAY AGAIN
- ยกเลิกรอบ: ส่งผล `ABORTED` แล้ว state กลับ `IDLE`

### ปรับค่า

ดูทั้งหมดด้วย `python -m engine --help`:

```powershell
python -m engine --limit 30 --intro 2 --count 3 --green 3 6 --red 3 5 --imu-acc 0.3 --imu-gyro 50
```

- **เกณฑ์ของข้อมือยังไม่ได้ปรับกับ ESP จริง:** 0.3 g และ 50 deg/s เป็นค่าเริ่มต้น ให้ดู `acc_peak` และ `gyro_rms` ตอนยืนนิ่งใน Grafana แล้วตั้งให้สูงกว่าค่านั้นพอสมควร
- **ถ้าเปลี่ยน `--imu-gyro`:** ต้องแก้ `GYRO_LIMIT_DPS` ใน `web/frontend/js/app.js` ด้วย เพื่อให้เส้นประในกราฟหน้าผลตรงกัน

## ข้อตกลงที่ต้องรู้

- **`round_id` เป็นตัวเลข:** ใช้เวลา epoch เป็นวินาที เพราะ `pi_client.py` จัดรูปแบบด้วย `:03d` ค่านี้ไม่ซ้ำแม้ปิดเปิด engine ใหม่ และ backend เก็บเป็นข้อความ
- **ส่ง `rlgl/game/result` ก่อน state สุดท้ายทุกครั้ง:** `pi_client.py` อ่านผลล่าสุดตอนได้ state `GAME_OVER` / `WIN`
- **ผลมีชื่อซ้ำ:** `cause`, `part`, `score` ซ้ำกับ `reason`, `caught_part`, `caught_score_ratio` เพราะ `pi_client.py` อ่านชื่อเหล่านั้น
- **การขยับจากกล้อง:** `pi_client.py` (28 พ.ย.) ส่งค่าเทียบเกณฑ์ในชื่อ `motion_score` engine, backend, หน้าเว็บ และ Grafana รับได้ทั้ง `score_ratio`, `score` และ `motion_score`
- **จบ calibrate ที่ Pi:** `pi_client.py` จบ calibrate ตอน state เปลี่ยนจาก `CALIBRATE` เป็นอะไรก็ได้ จึงใช้กับ `READY` และ `CALIBRATE_FAILED` ได้ ส่วนการ calibrate ใหม่ engine จะส่ง `CALIBRATE` อีกครั้งหลัง `CALIBRATE_FAILED` (state เปลี่ยน Pi จึงเริ่มใหม่)
- **device_id:** engine ใช้ `game-01` และตั้ง LWT ที่ `rlgl/status/game-01`
- **ถ้าปิด engine แบบกะทันหัน** (เช่นปิดหน้าต่าง): state สุดท้ายจะค้างใน broker และหน้าเว็บจะค้างที่หน้าเดิม เปิด engine ใหม่แล้ว engine จะส่ง `IDLE` ให้เอง

## หน้าเว็บที่เกี่ยวข้อง

| state | หน้าเว็บ |
|---|---|
| `CALIBRATE`, `CALIBRATE_FAILED`, `READY` | หน้า calibrate: ภาพกล้อง, แถบความคืบหน้า, รายการ sensor (✓ ผ่าน / … รอ / ✗ ไม่ผ่าน / NOT USED) และกล่องบอกสถานะ (CHECKING / CHECK FAILED / ALL SENSORS OK) |
| `COUNTDOWN` | หน้าเกม (หรือหน้า Sniper) มีภาพทับ: animation "GET READY!" → 3 → 2 → 1 → GO! |
| `WIN`, `GAME_OVER` | หน้าผล |

**กราฟ timeline ในหน้าผล:**
- **WRIST:** gyro ข้อมือ ตลอดรอบ มีเส้นประเกณฑ์ 50°/s
- **CAMERA:** การขยับจาก MediaPipe บน Pi เทียบเกณฑ์ เฉพาะช่วงไฟแดง (1.0 = แพ้)

## ทดสอบ

```powershell
cd laptop\engine
```

```powershell
python -m pytest
```

### ผลทดสอบ (28 พ.ย. 2026)

**unit test:** engine 56 ข้อ, backend 168 ข้อ, หน้าเว็บ 24 ข้อ ผ่านทั้งหมด

**ทดสอบทั้งระบบ** บน broker ทดสอบบน laptop กับ backend ชุดทดสอบ (port 8001) โดยใช้อุปกรณ์ปลอม:

| ทดสอบ | ผล |
|---|---|
| calibrate ตอนชีพจรเป็น 0 | ไม่ผ่าน หน้าเว็บขึ้น "Heart rate NO PULSE" และ CHECK FAILED |
| เปิดชีพจรแล้วกดใหม่ | calibrate ผ่าน ขึ้น ALL SENSORS OK |
| กดครั้งที่สอง | GET READY → 3 → 2 → GO! → ไฟเขียว เวลาเริ่มที่ 00:30 |
| ขยับข้อมือตอนไฟแดง | แพ้ (`imu`) กราฟหน้าผลแสดง gyro ทั้งรอบ และกล้องเฉพาะไฟแดง |
| โหมด Sniper | นับถอยหลังบนหน้า Sniper แล้วเข้าเกม |

**ยังไม่ได้ทดสอบ:** เล่นจริงกับ Pi, ESP และ touch sensor
