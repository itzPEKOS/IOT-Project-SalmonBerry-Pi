# สรุปสถาปัตยกรรมระบบ: IoT Red Light, Green Light

> อัปเดต 26 พ.ย. 2026 · ใช้คู่กับ `spec.md`
> **สถานะ:** ใช้ **broker ตัวเดียวบน Pi** (ตัด MQTT bridge ออกแล้ว) ถ้า Pi รับไม่ไหวให้ใช้ **แผนสำรอง** (ย้าย broker ไปที่โน้ตบุ๊ก)

---

## 1. บทบาทของแต่ละอุปกรณ์

| อุปกรณ์ | Platform ตามเกณฑ์ | ทำอะไร | MQTT |
| :--- | :--- | :--- | :--- |
| **ESP1 (ข้อมือ)** | Sensor Node | อ่าน Heartbeat + Gyro (MPU6050) | Client: publish |
| **ESP2 (สถานี)** | Sensor Node | Joystick, คุม Pan-Tilt Servo + Red LED | Client: publish + subscribe คำสั่ง |
| **Raspberry Pi** | Gateway | กล้อง + pose model, Ultrasonic, Touch, รวมข้อมูลจาก ESP | **Broker (Mosquitto ตัวเดียวของระบบ)** + Client |
| **โน้ตบุ๊ก** | On-premise Server | Game Engine, InfluxDB, Web + Grafana, เรียก Gemini | Client (ต่อเข้า broker บน Pi) |

* **ESP32 ไม่ใช่ broker** เป็นแค่ MQTT client
* **Sensor Node** = ไมโครคอนโทรลเลอร์กินไฟน้อย ทำหน้าที่เก็บข้อมูลหรือคุม actuator ที่ปลายทาง
* **Gateway** = คอมพิวเตอร์เล็กที่รัน Linux อยู่ระหว่างอุปกรณ์กับ server รวมข้อมูลและประมวลผลก่อนส่งต่อ (edge processing)

---

## 2. แผนหลัก: Broker เดียวบน Pi

```
ESP1, ESP2 ──────────────┐
สคริปต์ pose บน Pi ──────┼──► Mosquitto บน Pi ◄── Game Engine, Telegraf (→ InfluxDB), backend, เว็บ
(localhost)              ┘                         (อยู่บนโน้ตบุ๊ก ต่อเข้ามาทาง LAN)
```

ทุกอย่างต่อเข้า broker ตัวเดียวที่ Pi ไม่มี bridge และไม่มีข้อความที่ต้องข้ามสอง broker

* **ESP1, ESP2:** `MQTT_HOST` = IP ของ Pi
* **สคริปต์ pose บน Pi:** ต่อ `localhost`
* **Game Engine, Telegraf, backend, เว็บ (โน้ตบุ๊ก):** ต่อ IP ของ Pi
* ตั้ง IP ของ Pi ให้คงที่ (reserve ที่ router) เพราะทุกตัวพึ่ง IP นี้

### ทำไม Game Engine ยังอยู่ที่โน้ตบุ๊ก ไม่อยู่บน Pi
* Pi ต้องรันโมเดลภาพซึ่งกิน CPU มากอยู่แล้ว ถ้าเพิ่ม Game Engine การตัดสินจะหน่วงไปทั้งระบบ
* แยกบทบาท gateway กับ server ออกจากกัน

### ข้อควรระวังของ broker เดียวบน Pi
* **Pi เป็นจุดล้มเหลวเดียว** ถ้า Pi ค้างตอนรันโมเดล ทั้ง ESP และ Game Engine จะหลุดพร้อมกัน ให้จำกัด CPU ของสคริปต์ pose (ลดความละเอียด/FPS) เพื่อให้ Mosquitto ยังตอบได้
* ตั้ง LWT ให้ทุกอุปกรณ์ เพื่อให้หน้าเว็บเห็นทันทีว่าใครหลุด และ Game Engine ต้อง Pause เกมเมื่อ Pi (broker) หลุด
* ทุก topic ยังขึ้นต้นด้วย `rlgl/` เหมือนเดิม (เพื่อให้ย้ายกลับไปใช้ bridge หรือแผนสำรองได้ง่าย)

### Config

ไฟล์จริงอยู่ที่ `pi/mosquitto/rlgl.conf` และติดตั้งบน Pi ได้ด้วย `bash pi/setup_mosquitto.sh` (ติดตั้ง Mosquitto, คัดลอก config, รีสตาร์ต, ทดสอบ pub/sub และพิมพ์ IP ของ Pi) ส่วนด้านล่างเป็นสรุปเนื้อหา

**Pi: `/etc/mosquitto/conf.d/rlgl.conf`** (Mosquitto 2.x ถ้าไม่ใส่ `listener` กับ `allow_anonymous` อุปกรณ์อื่นจะต่อไม่ได้)
```
listener 1883 0.0.0.0
protocol mqtt
allow_anonymous true

# WebSocket สำหรับหน้าเว็บ (เบราว์เซอร์ต่อ MQTT ตรงๆ ไม่ได้)
listener 9001 0.0.0.0
protocol websockets
```
* เปิด port 1883 และ 9001 บน Pi ถ้ามี firewall
* ถ้าจะใช้รหัสผ่าน ให้ตั้ง `password_file` บน Pi และให้ `MQTT_USER` / `MQTT_PASSWORD` ใน `laptop/infra/.env` ตรงกัน
* **โน้ตบุ๊กไม่ต้องรัน Mosquitto** (ในโฟลเดอร์ `laptop/infra` มี broker สำหรับพัฒนาที่ปิดได้ด้วยการลบ `COMPOSE_PROFILES=devbroker` ออกจาก `.env`)

### ขั้นตอนทดสอบ
1. Pi: `sudo systemctl restart mosquitto` แล้วเช็ก `systemctl status mosquitto`
2. ทดสอบจากโน้ตบุ๊กไป Pi
   * Pi: `mosquitto_sub -h localhost -t "rlgl/#" -v`
   * โน้ตบุ๊ก: `mosquitto_pub -h <IP Pi> -t rlgl/test -m "hello from notebook"` → ต้องเห็นข้อความขึ้นบน Pi
3. ทดสอบจาก Pi ไปโน้ตบุ๊ก
   * โน้ตบุ๊ก: `mosquitto_sub -h <IP Pi> -t "rlgl/#" -v`
   * Pi: `mosquitto_pub -h localhost -t rlgl/test -m "hello from pi"` → ต้องเห็นข้อความขึ้นบนโน้ตบุ๊ก
4. ทดสอบเว็บ: เปิดหน้าเว็บแล้วดู Device Status ว่าเห็น ESP1, ESP2, Pi เป็น online
5. ทดสอบหลุด: ปิด Pi ทั้งเครื่อง แล้วดูว่าหน้าเว็บและ Game Engine Pause เกมตามที่ออกแบบ

---

## 3. แผนสำรอง: ย้าย broker ไปโน้ตบุ๊ก

ใช้เมื่อ Pi ไม่เสถียร หรืออาจารย์บอกว่า message queue ต้องอยู่ที่ server

```
ESP1, ESP2, Pi ──► Mosquitto บนโน้ตบุ๊ก ──► Game Engine, InfluxDB, เว็บ
```

**วิธีสลับ:** รัน Mosquitto บนโน้ตบุ๊ก (เปิด `listener 1883 0.0.0.0`, `allow_anonymous true`, WebSocket 9001 และเปิด port ใน Windows Firewall) แล้วเปลี่ยน IP ของ broker ใน ESP1, ESP2, สคริปต์บน Pi, Telegraf และ backend ให้ชี้ไปที่โน้ตบุ๊ก ส่วนโค้ด Game Engine ไม่ต้องแก้

**ข้อตกลงในการเขียนโค้ด:** ทุกตัวต้องอ่าน IP ของ broker จาก config ที่เดียว จะได้สลับแผนโดยไม่ต้องแก้โค้ด
* ESP: `const char* MQTT_HOST` ในส่วน CONFIG บนสุดของ `.ino`
* Pi / โน้ตบุ๊ก: อ่านจาก `config.yaml` หรือ `.env`
* เว็บ: `RLGL_STREAM_URL` และ `RLGL_MQTT_HOST` ใน `laptop/infra/.env` (แก้ทั้งหมดด้วย `laptop/infra/set_broker_ip.py`)
* ตั้ง IP โน้ตบุ๊กและ Pi ให้คงที่ (reserve ที่ router) จะได้ไม่ต้องแก้ใหม่ในวันเดโม

---

## 4. ใครคำนวณอะไร

| งาน | ทำที่ | เหตุผล |
| :--- | :--- | :--- |
| รัน pose model + เทียบ keypoint กับท่าตั้งต้น → ตัดสินว่า "ขยับ / ส่วนไหน" | **Pi** | ดีเลย์ต่ำ ส่งข้อมูลน้อย การเทียบ keypoint ใช้ไม่ถึง 1 ms งานหนักคือตัวโมเดล |
| ตัดสิน `GAME_OVER` / `WIN` (state, grace period, fusion `gyro > thr OR Pi ขยับ`, touch) | **โน้ตบุ๊ก (Game Engine)** | รวมข้อมูลจากทุกแหล่งได้ที่เดียว |
| บันทึก InfluxDB, Dashboard, เรียก Gemini | **โน้ตบุ๊ก** | |

* Pi ส่งแค่ผลสรุป เช่น `{"moved": true, "part": "left_arm", "score": 0.08}`
* **Pi ต้อง subscribe state ของเกม** เพื่อเก็บท่าตั้งต้นตอนเข้า `RED_LIGHT` หลังจบ grace period (ไม่จับเวลาเอง)
* วิดีโอสำหรับหน้าเว็บส่งแยกผ่าน HTTP (เช่น MJPEG) **ไม่ส่งผ่าน MQTT**

---

## 5. Flow ของเกม

1. **Idle / Calibrate:** เช็กว่าอุปกรณ์ออนไลน์ครบ เก็บ baseline ของ gyro และ BPM สั่ง servo หันหลัง LED ดับ
2. **Green light:** ผู้เล่นเดิน Pi ส่งระยะจาก ultrasonic ไปอัปเดต progress bar แตะ touch ตอนนี้ = **WIN**
3. **Grace period (0.5–1 s):** servo หันกลับ LED ติดค้าง ยังไม่จับแพ้
4. **Red light:** Pi เก็บท่าตั้งต้นแล้วส่งผลการขยับ Game Engine เช็ก gyro ไปพร้อมกัน ถ้าฝั่งใดฝั่งหนึ่งพบการขยับ หรือแตะ touch = **GAME_OVER** ถ้านิ่งจนหมดเวลาก็กลับไปขั้นที่ 2
5. **Game over:** LED กะพริบ หน้าเว็บแสดงสาเหตุที่แพ้
6. **จบเกม (ชนะหรือแพ้):** บันทึกสถิติลง InfluxDB แล้วส่งให้ Gemini พากย์ (timeout 3–5 s ถ้าไม่สำเร็จใช้ข้อความสำเร็จรูป)

---

## 6. AI / Vision

* ตัวเลือกโมเดลบน Pi: **MediaPipe Pose Landmarker (lite)** เป็นตัวหลัก เพราะผู้เล่นมีคนเดียว ลงบน Pi ง่าย ไม่ต้องใช้ PyTorch / **YOLO26n-pose** เป็นตัวเทียบ / **Frame differencing** เป็น baseline
* ตัดสินใจจาก FPS และความแม่นที่วัดบน Pi จริง (`pi/test_motion.py` บันทึกผลลง `captures/benchmark.csv`)
* วันเดโมจัดกล้องให้เห็นแค่พื้นที่เล่น กันไม่ให้ MediaPipe ไปจับคนอื่นที่เดินผ่าน

---

## 7. สถานะเทียบเกณฑ์ (2110575) และสิ่งที่ต้องทำ

| ข้อ | สถานะ |
| :--- | :--- |
| 1–5 Sensor / Actuator | ✅ ⚠️ ข้อ 1: ถ้าอาจารย์ไม่นับกล้องและ joystick จะเหลือ 4 ชนิด ควรพิจารณาเพิ่ม sensor ราคาถูกอีกตัว |
| 6, 7.1, 8 | ✅ |
| 7.2 AI service | ✅ ในแผน (ยังต้องเขียนส่วนเรียก Gemini) ทางเลือกเพิ่ม: ส่งภาพเฟรมที่แพ้ให้ Gemini Vision อธิบาย |
| 9.1 FPS + accuracy | ❌ ต้องเก็บชุดทดสอบ เช่น ไฟแดง 40 รอบ (นิ่ง 20 / ขยับ 20) |
| 9.2 เคสถูก / พลาด | ❌ เซฟภาพด้วยปุ่ม `s` |
| 10 MQTT ≥ 3 topics + อธิบาย (3 คะแนน) | ⚠️ ยังไม่ได้ทำตาราง topic |
| 11 Server | ⚠️ โน้ตบุ๊ก: Game Engine subscribe และประมวลผลข้อมูลจาก broker บน Pi (ถ้าอาจารย์ต้องการให้ message queue อยู่ที่ server ให้ใช้แผนสำรอง) |
| 12, 13 | ✅ ในแผน |
| 14 สไลด์ / 15 วิดีโอ | ❌ ส่งก่อน 9:00 น. 2 ธ.ค. 2026 |

**คำถามที่ควรถามอาจารย์หรือ TA:** ข้อ 11 ต้องให้ broker (message queue) อยู่บนฝั่ง server หรือไม่ ถ้าบอกว่าต้อง ให้สลับไปแผนสำรองในหัวข้อ 3
