# CLAUDE.md

บริบทของโปรเจกต์สำหรับ Claude อ่านก่อนเริ่มทำงานทุกครั้ง

## โปรเจกต์

เกม **Red Light, Green Light** ด้วย IoT เป็นงานกลุ่มวิชา **2110575 IoT Systems and Digital Solutions (2026/1)** จุฬาฯ
ผู้เล่นมีคนเดียว ตอนไฟเขียวให้เดินเข้าหาเส้นชัย ตอนไฟแดงต้องนิ่ง ระบบตัดสินแพ้ชนะจากกล้อง (pose model) และ gyro ที่ข้อมือ

**ส่งงาน:** สไลด์และวิดีโอ (~5 นาที) ใน myCourseVille **ก่อน 9:00 น. วันที่ 2 ธ.ค. 2026** ตั้งเป้าให้ระบบเสร็จภายใน 28 พ.ย.

## เอกสารหลัก (อ่านก่อนแก้ระบบ)

| ไฟล์ | เนื้อหา |
| :--- | :--- |
| `docs/spec.md` | สเปก: ฮาร์ดแวร์, state machine, dashboard, edge cases, checklist เกณฑ์ |
| `docs/ARCHITECTURE.md` | การตัดสินใจล่าสุด: broker เดียวบน Pi, ใครคำนวณอะไร, แผนสำรอง, สถานะเทียบเกณฑ์ 25 คะแนน |

ถ้าสองไฟล์ขัดกัน ให้ยึด `docs/ARCHITECTURE.md` (ใหม่กว่า) และแก้ให้ตรงกัน

## สถาปัตยกรรม (สรุป)

| อุปกรณ์ | Platform | หน้าที่ |
| :--- | :--- | :--- |
| ESP1 (ข้อมือ) | Sensor Node | Heartbeat + MPU6050 (I2C) |
| ESP2 (สถานี) | Sensor Node | Joystick, Pan-Tilt Servo ×2, Red LED ดวงเดียว (ดับ/ติดค้าง/กะพริบ) |
| Raspberry Pi | Gateway | กล้อง + MediaPipe Pose, Ultrasonic HC-SR04, Touch, **Mosquitto (broker เดียวของระบบ)** |
| โน้ตบุ๊ก | On-premise server | Game Engine, InfluxDB, เว็บของทีม + Grafana (iframe), เรียก Gemini |

* **MQTT (แผนหลัก):** broker ตัวเดียวบน Pi ESP, สคริปต์บน Pi และบริการบนโน้ตบุ๊ก (Game Engine, Telegraf, backend, เว็บ) ต่อเข้า broker ตัวนี้ทั้งหมด ไม่มี bridge
* **แผนสำรอง:** ย้าย broker ไปรันบนโน้ตบุ๊ก แล้วเปลี่ยน IP ของทุกตัวให้ชี้ไปที่โน้ตบุ๊ก (ใช้ถ้า Pi ไม่เสถียร หรืออาจารย์ต้องการให้ broker อยู่ฝั่ง server)
* **Pi** ตัดสินเฉพาะเรื่องภาพ แล้ว publish ผลสรุป เช่น `{"moved": true, "part": "left_arm", "score": 0.08}`
* **Game Engine บนโน้ตบุ๊ก** เป็นคนตัดสิน `GAME_OVER` / `WIN`: state + grace period + `gyro > thr OR Pi ขยับ` + touch
* Pi ต้อง subscribe state ของเกม เพื่อเก็บท่าตั้งต้นตอนเข้า `RED_LIGHT` (ไม่จับเวลาเอง)
* วิดีโอสำหรับหน้าเว็บส่งผ่าน HTTP (MJPEG) **ไม่ส่งผ่าน MQTT**
* **ไม่ใช้ไมค์และไม่มี DHT11** ในระบบ

## ไฟล์โค้ดตอนนี้

| ไฟล์ | ใช้ทำอะไร |
| :--- | :--- |
| `esp32/wrist/wrist.ino`, `esp32/finish/finish.ino` | ESP ข้อมือ และ ESP เส้นชัย (ค่า Wi-Fi / IP broker อยู่ในส่วน CONFIG บนสุดของไฟล์) |
| `pi/pi_client.py` | รันบน Pi: กล้อง + MediaPipe → MQTT `rlgl/gateway/vision`, ภาพสด `:8000/stream`, อัดคลิปส่ง backend |
| `pi/motion_detector.py` | ตัวตรวจการขยับที่ `pi_client.py` ใช้ |
| `laptop/engine/` | game engine: state ของเกม และตัดสินแพ้ชนะ |
| `laptop/web/` | backend (FastAPI) + หน้าเว็บของผู้เล่นและ scoreboard |
| `laptop/infra/` | Docker: InfluxDB, Telegraf, Grafana |
| `pi/test_motion.py` | ทดสอบบนโน้ตบุ๊กด้วยเว็บแคม: `--method` mediapipe / pose / diff, แถบ score แยกส่วน, ปรับ thr สด (`+`/`-`, `[`/`]`), วัด FPS/CPU/RAM, บันทึก `captures/benchmark.csv` |
| `tools/pi_detect.py` | รันบน Pi: กล้อง CSI (Picamera2) หรือ USB, จำลองรอบไฟเขียว/แดงเอง, พิมพ์ผลทาง terminal (ใช้ผ่าน SSH ได้) |

* `pi/pi_client.py` และ `tools/pi_detect.py` import ฟังก์ชันจาก `pi/test_motion.py` (`MediaPipePose`, `draw_mp_all`, `put` ฯลฯ) ถ้าแก้ signature ใน `test_motion.py` ต้องเช็กสองไฟล์นี้ด้วย (`tools/` หาไฟล์ใน `pi/` เองผ่าน `sys.path`)
* โมเดลหลัก: **MediaPipe Pose Landmarker lite** (ผู้เล่นคนเดียว, ลงบน Pi ง่าย) / YOLO26n-pose ไว้เทียบ / frame differencing เป็น baseline

## Logic คำนวณการขยับ (ห้ามเปลี่ยนโดยไม่บอกทีม)

1. ตอนเข้าไฟแดงเก็บท่าตั้งต้น: keypoint 17 จุด (รูปแบบ COCO) + confidence + ความสูงตัว
2. ทุกเฟรม: `d_k = ระยะที่จุด k เลื่อนจากท่าตั้งต้น / ความสูงตัว` ใช้เฉพาะจุดที่ confidence > 0.5 ทั้งสองเฟรม
3. เฉลี่ยตาม 6 ส่วน (head, left_arm, right_arm, torso, left_leg, right_leg)
4. `score` = ค่าสูงสุดของ 6 ส่วน, `part` = ชื่อส่วนนั้น
5. ขยับ = `score > thr` ติดกัน `frames` เฟรม (ค่าเริ่มต้น thr 0.05, frames 3)

MediaPipe มี 33 จุด แปลงเหลือ 17 ด้วย `MP_TO_COCO` เพื่อให้ใช้สูตรเดียวกับ YOLO

## สภาพแวดล้อม

* **โน้ตบุ๊ก (Windows):**
  ```
  conda create -n rlgl python=3.11 -y
  conda activate rlgl
  pip install ultralytics mediapipe opencv-python psutil
  ```
  ถ้า `import cv2` error: ถอน `opencv-python` และ `opencv-contrib-python` แล้วลง `opencv-contrib-python` อย่างเดียว
* **Raspberry Pi (Pi OS 64-bit):**
  ```
  sudo apt install -y python3-picamera2
  python3 -m venv --system-site-packages ~/rlgl-venv
  source ~/rlgl-venv/bin/activate
  pip install mediapipe psutil
  ```
  ห้ามลง `opencv-python` แยกบน Pi (mediapipe ลง `opencv-contrib-python` มาให้แล้ว)
* Mosquitto 2.x ต้องมี `listener 1883 0.0.0.0` และ `allow_anonymous true` ไม่งั้นเครื่องอื่นต่อไม่ได้

## ข้อตกลงในการเขียนโค้ด

* เอกสาร คอมเมนต์ และข้อความถึงผู้ใช้เป็น**ภาษาไทย** ชื่อตัวแปร ฟังก์ชัน และ topic เป็นภาษาอังกฤษ
* ข้อความที่วาดบนภาพด้วย `cv2.putText` ต้องเป็นภาษาอังกฤษ (OpenCV แสดงภาษาไทยไม่ได้)
* IP ของ broker อ่านจาก config ที่เดียว: ESP ใช้ `MQTT_HOST` ในส่วน CONFIG บนสุดของ `.ino`, laptop ใช้ `laptop/infra/.env`
* MQTT topic ขึ้นต้นด้วย `rlgl/` เสมอ
* โค้ดบน ESP ห้ามใช้ `delay()` ใน loop หลัก (โดยเฉพาะตอนกะพริบ LED) ใช้ `millis()` เพื่อให้ `client.loop()` ทำงานตลอด
* อย่า commit ไฟล์โมเดลหรือผลทดสอบ: `*.pt`, `*.task`, `captures/`, `replays/` (อยู่ใน `.gitignore` แล้ว) และอย่า commit รหัส Wi-Fi จริงใน `.ino` (ใส่ `YOUR_PASSWORD` ไว้ใน repo)

## เกณฑ์ที่ยังไม่ผ่าน / งานค้าง

* **ข้อ 9.1:** accuracy ยังไม่มี ต้องเก็บชุดทดสอบ เช่น ไฟแดง 40 รอบ (นิ่ง 20 / ขยับ 20) และวัด FPS บน Pi จริง
* **ข้อ 9.2:** ภาพเคสที่จับถูกและจับพลาด (`captures/`)
* **ข้อ 10 (3 คะแนน):** ยังไม่มีตาราง MQTT topic พร้อมคำอธิบายหน้าที่ของแต่ละ topic
* **ข้อ 7.2:** ยังไม่ได้เขียนส่วนเรียก Gemini (timeout 3–5 วินาที + ข้อความสำรอง)
* **ข้อ 1 (ความเสี่ยง):** ถ้าอาจารย์ไม่นับกล้องและ joystick เป็น sensor จะเหลือ 4 ชนิด
* **ข้อ 14–15:** สไลด์และวิดีโอ
* **ถามอาจารย์/TA:** ข้อ 11 ต้องให้ broker (message queue) อยู่ฝั่ง server หรือไม่ (ถ้าต้อง ให้สลับไปแผนสำรอง)
