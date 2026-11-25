# สรุปสถาปัตยกรรมระบบ: IoT Red Light, Green Light

> อัปเดต 25 พ.ย. 2026 · ใช้คู่กับ `red_light_green_light_iot_spec.md`
> **สถานะ:** กำลังทดสอบ **แผนหลัก (MQTT Bridge ผ่าน Pi)** ถ้า Pi รับไม่ไหวให้ใช้ **แผนสำรอง** (ESP ต่อโน้ตบุ๊กตรง)

---

## 1. บทบาทของแต่ละอุปกรณ์

| อุปกรณ์ | Platform ตามเกณฑ์ | ทำอะไร | MQTT |
| :--- | :--- | :--- | :--- |
| **ESP1 (ข้อมือ)** | Sensor Node | อ่าน Heartbeat + Gyro (MPU6050) | Client: publish |
| **ESP2 (สถานี)** | Sensor Node | Joystick, คุม Pan-Tilt Servo + Red LED | Client: publish + subscribe คำสั่ง |
| **Raspberry Pi** | Gateway | กล้อง + pose model, Ultrasonic, Touch, รวมข้อมูลจาก ESP แล้วส่งต่อ | Broker ตัวเล็ก (bridge) + Client |
| **โน้ตบุ๊ก** | On-premise Server | Broker หลัก, Game Engine, InfluxDB, Web + Grafana, เรียก Gemini | Broker หลัก + Client |

* **ESP32 ไม่ใช่ broker** เป็นแค่ MQTT client
* **Sensor Node** = ไมโครคอนโทรลเลอร์กินไฟน้อย ทำหน้าที่เก็บข้อมูลหรือคุม actuator ที่ปลายทาง
* **Gateway** = คอมพิวเตอร์เล็กที่รัน Linux อยู่ระหว่างอุปกรณ์กับ server รวมข้อมูลและประมวลผลก่อนส่งต่อ (edge processing)

---

## 2. แผนหลัก: MQTT Bridge ผ่าน Pi

```
ESP1, ESP2 ──► Mosquitto บน Pi ──(bridge)──► Mosquitto บนโน้ตบุ๊ก ──► Game Engine, InfluxDB, เว็บ
สคริปต์ pose บน Pi ──► localhost ──┘
```

**Bridge คืออะไร:** Mosquitto บน Pi ทำตัวเป็น client ของ Mosquitto บนโน้ตบุ๊ก แล้วคัดลอกข้อความใน topic `rlgl/#` ข้ามไปให้อัตโนมัติทั้งสองทาง ข้อมูลไม่ได้เก็บค้างไว้ที่ Pi แค่วิ่งผ่าน

* **ขาขึ้น:** `ESP1 publish rlgl/... → broker บน Pi → bridge → broker บนโน้ตบุ๊ก → Game Engine`
* **ขาลง:** `Game Engine publish rlgl/... → broker บนโน้ตบุ๊ก → bridge → broker บน Pi → ESP2`
* Game Engine subscribe ที่โน้ตบุ๊กอย่างเดียว ไม่ต้องรู้ว่าข้อความผ่าน Pi มาก่อน **โค้ด Game Engine จึงเหมือนกันทั้งแผนหลักและแผนสำรอง**
* ดีเลย์เพิ่มแค่ไม่กี่ ms ในวง LAN เดียวกัน และ bridge แทบไม่ใช้ CPU ของ Pi
* Mosquitto กันข้อความวนไปกลับระหว่างสองฝั่งให้เอง (`try_private` เปิดเป็นค่าเริ่มต้น)

### ทำไม Game Engine อยู่ที่โน้ตบุ๊ก ไม่อยู่บน Pi
* Pi ต้องรันโมเดลภาพซึ่งกิน CPU มากอยู่แล้ว ถ้าเพิ่ม Game Engine การตัดสินจะหน่วงไปทั้งระบบ
* ให้เกณฑ์ข้อ 11 (server receive and process data from message queue) ชัดเจน และแยกบทบาท gateway กับ server ออกจากกัน

### Config

**Pi: `/etc/mosquitto/conf.d/bridge.conf`**
```
listener 1883 0.0.0.0
allow_anonymous true

connection to-notebook
address <IP โน้ตบุ๊ก>:1883
topic rlgl/# both 0
cleansession true
```

**โน้ตบุ๊ก: `mosquitto.conf`** (Mosquitto 2.x ถ้าไม่ใส่สองบรรทัดนี้ อุปกรณ์อื่นจะต่อไม่ได้)
```
listener 1883 0.0.0.0
allow_anonymous true
```
และต้องเปิด port 1883 ใน Windows Firewall

### ขั้นตอนทดสอบ bridge
1. โน้ตบุ๊ก: `mosquitto -c mosquitto.conf -v`
2. Pi: `sudo systemctl restart mosquitto`
3. ทดสอบขาขึ้น
   * โน้ตบุ๊ก: `mosquitto_sub -h localhost -t "rlgl/#" -v`
   * Pi: `mosquitto_pub -h localhost -t rlgl/test -m "hello from pi"` → ต้องเห็นข้อความขึ้นบนโน้ตบุ๊ก
4. ทดสอบขาลง
   * Pi: `mosquitto_sub -h localhost -t "rlgl/#" -v`
   * โน้ตบุ๊ก: `mosquitto_pub -h localhost -t rlgl/test -m "hello from notebook"` → ต้องเห็นข้อความขึ้นบน Pi
5. ตรวจว่า Pi หลุดหรือยัง: bridge จะแจ้งสถานะการเชื่อมต่อ (1 = ต่ออยู่, 0 = หลุด) ไปที่โน้ตบุ๊กให้เอง ดูได้ด้วย `mosquitto_sub -h localhost -t '$SYS/broker/connection/#' -v`

---

## 3. แผนสำรอง: ESP ต่อโน้ตบุ๊กตรง

ใช้เมื่อ Pi ไม่เสถียร หรืออาจารย์บอกว่าไม่จำเป็นต้องให้ sensor node เชื่อมผ่าน gateway

```
ESP1, ESP2, Pi ──► Mosquitto บนโน้ตบุ๊ก ──► Game Engine, InfluxDB, เว็บ
```

**วิธีสลับ:** เปลี่ยน IP ของ broker ใน ESP1, ESP2 และสคริปต์บน Pi ให้ชี้ไปที่โน้ตบุ๊ก แล้วปิด bridge บน Pi ส่วน Game Engine ไม่ต้องแก้อะไร

**ข้อตกลงในการเขียนโค้ด:** ทุกตัวต้องอ่าน IP ของ broker จาก config ที่เดียว จะได้สลับแผนโดยไม่ต้องแก้โค้ด
* ESP: `#define MQTT_HOST "..."` ไว้บนสุดของไฟล์
* Pi / โน้ตบุ๊ก: อ่านจาก `config.yaml` หรือ `.env`
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
* ตัดสินใจจาก FPS และความแม่นที่วัดบน Pi จริง (`test_motion.py` บันทึกผลลง `captures/benchmark.csv`)
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
| 11 Server | ✅ โน้ตบุ๊ก: Game Engine subscribe และประมวลผล |
| 12, 13 | ✅ ในแผน |
| 14 สไลด์ / 15 วิดีโอ | ❌ ส่งก่อน 9:00 น. 2 ธ.ค. 2026 |

**คำถามที่ควรถามอาจารย์หรือ TA:** Sensor Node ต้องเชื่อมผ่าน Gateway หรือไม่ (ถ้าไม่จำเป็น ใช้แผนสำรองได้เลย)
