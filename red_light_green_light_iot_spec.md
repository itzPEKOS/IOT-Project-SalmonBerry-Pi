# Project Specification: IoT Red Light, Green Light Game

> **Context for AI / Developer:** เอกสารนี้คือสเปกของโปรเจกต์วิชา **2110575 IoT Systems and Digital Solutions** ใช้เป็นบริบทหลักสำหรับการออกแบบสถาปัตยกรรมระบบ (MQTT Topics, Database Schema, API) และการเขียนโค้ดต่อไป

> **อัปเดต 25 พ.ย. 2026:** ย้าย Ultrasonic จาก ESP2 ไปต่อที่ Raspberry Pi / ไม่ใช้ไมค์ในระบบ / Dashboard เป็นเว็บของทีมเอง และฝังกราฟจาก Grafana

---

## 1. ภาพรวมฮาร์ดแวร์และสถาปัตยกรรมระบบ (Hardware & Platform Mapping)

ออกแบบให้ครอบคลุมเกณฑ์วิชา (เซนเซอร์ ≥ 5 ชนิด, ใช้ I2C ≥ 1 ตัว, Sensor Node ≥ 2 เซนเซอร์, Gateway ≥ 2 เซนเซอร์, Actuator ≥ 1 ตัว)

1. **ESP1 (Sensor Node - สวมที่ข้อมือผู้เล่น / Wearable)**
   * **Heartbeat Sensor:** วัดชีพจรผู้เล่น (BPM) *(I2C เช่น MAX30102 หรือ Analog)*
   * **Gyroscope Sensor:** วัดความสั่น/การขยับของข้อมือ *(I2C เช่น MPU6050)*

2. **ESP2 (Sensor Node - สถานีควบคุมใกล้คอมพิวเตอร์)**
   * **Actuator Motor:** Pan-Tilt (MG90S Servo 2 ตัว) สำหรับหันกล้อง/ตัวตุ๊กตา *(ต้องแยกแหล่งจ่ายไฟ 5V และต่อ GND ร่วม)*
   * **Joystick Module:** ใช้บังคับ Pan-Tilt Servo ในโหมด Manual
   * **Red LED (สีแดงดวงเดียว เปลี่ยนสีไม่ได้):** ใช้สื่อสารสถานะเกมให้ผู้เล่นเห็นชัดเจน
     * **ดับ (OFF):** ตอนกล้องหันหลัง (`IDLE`, `GREEN_LIGHT`, `WIN`)
     * **ติดสว่างค้าง (SOLID ON):** ตอนกล้องหันหน้ามาหาผู้เล่น (`TRANSITION`, `RED_LIGHT`)
     * **กะพริบต่อเนื่อง (BLINKING):** ตอนผู้เล่นแพ้ (`GAME_OVER`)

3. **Raspberry Pi (Gateway Platform)**
   * **Camera Module / Video Stream:** ถ่ายภาพผู้เล่น รัน AI Model ตรวจจับการขยับ (Keypoint/Motion Detection) และคำนวณค่า FPS
   * **Ultrasonic Sensor (HC-SR04):** วัดระยะห่างของผู้เล่นเทียบกับเส้นชัย *(ขา Echo เป็น 5V ต้องผ่าน Voltage Divider เช่น 1kΩ/2kΩ ก่อนเข้า GPIO 3.3V)*
   * **Touch Module (เส้นชัย):** ต่อกับขา GPIO ของ Pi ใช้ตัดสินการชนะ

4. **Computer (On-Premise Server)**
   * รัน **Game Engine** (คุม State Machine ทั้งหมด)
   * รัน **MQTT Broker** (รองรับอย่างน้อย 3 Topics แยกตามแหล่งข้อมูล)
   * รัน **Time-Series Database** (InfluxDB) สำหรับเก็บข้อมูลเซนเซอร์ย้อนหลัง
   * รัน **Web Dashboard** ของทีมเอง (หน้าเกม, วิดีโอ, ปุ่มควบคุม) และ **Grafana** สำหรับกราฟ Real-time/Historical ที่ฝังเข้าหน้าเว็บด้วย iframe
   * เชื่อมต่อ **AI Service (Cloud API)** (เช่น Gemini API / OpenAI API / TTS) สำหรับสร้างคำพากย์/บทวิเคราะห์หลังจบเกม

---

## 2. Logic การเล่นเกม (State Machine)

Game Engine บนคอมพิวเตอร์ควบคุมสถานะเกม 5 สถานะผ่าน MQTT ดังนี้:

1. **`IDLE / CALIBRATE` (เตรียมความพร้อม)**
   * ตรวจสอบสถานะ Online ของ ESP1, ESP2 และ Raspberry Pi ผ่าน MQTT
   * เก็บค่าพื้นฐาน (Baseline) ของ Heartbeat และ Gyro จาก ESP1
   * สั่ง ESP2: หัน Pan-Tilt Servo กลับหลัง และสั่ง **Red LED = ดับ (OFF)**

2. **`GREEN_LIGHT` (ไฟเขียว - เดินได้)**
   * หน้าเว็บแสดงสถานะไฟเขียว เริ่มนับเวลาถอยหลัง และสั่ง **Red LED = ดับ (OFF)** (กล้องหันหลัง)
   * เริ่มบันทึกข้อมูลเซนเซอร์ (BPM, Gyro, Distance) ลง Time-Series DB ต่อเนื่อง
   * **Raspberry Pi (Ultrasonic)** ส่งระยะทางมาอัปเดต Progress Bar บนหน้าเว็บ
   * **ESP1 (Heartbeat)** ส่งค่าชีพจร (ใช้เป็นลูกเล่นได้ เช่น หากชีพจรพุ่งสูงเกินเกณฑ์ เกมจะสุ่มตัดเข้าไฟแดงเร็วขึ้น)
   * **เงื่อนไขชนะ:** หากผู้เล่นแตะ **Touch Module (บน Pi)** ในสถานะนี้ → เปลี่ยนเป็น `WIN` ทันที

3. **`TRANSITION / GRACE_PERIOD` (ช่วงเปลี่ยนผ่าน 0.5–1.0 วินาที)**
   * เกิดขึ้นทันทีเมื่อเปลี่ยนจากไฟเขียวเป็นไฟแดง ขณะที่ Servo กำลังหันหน้ากลับมา (หรือผู้คุมกำลังหมุน Joystick)
   * สั่ง **Red LED = ติดสว่างค้าง (SOLID ON)** เพื่อให้ผู้เล่นเห็นสัญญาณเตือนและเตรียมหยุด
   * **ยังไม่จับแพ้ในช่วงนี้** เพื่อเผื่อเวลาให้ผู้เล่นหยุดตัวทันและชดเชยดีเลย์ของเครือข่าย

4. **`RED_LIGHT` (ไฟแดง - ห้ามขยับ)**
   * Servo หันหน้ามาทางผู้เล่น และ **Red LED = ติดสว่างค้าง (SOLID ON)**
   * เปิดระบบตรวจจับแบบ **Sensor Fusion** (ทำงานร่วมกัน 2 ทาง):
     * **ESP1 (Gyro):** ตรวจจับความสั่น/มุมหมุนของข้อมือ หากเกิน Threshold = ขยับ
     * **Raspberry Pi (AI Model):** รันโมเดลจับการขยับร่างกายจากกล้อง หากพบการขยับ = ขยับ
   * **เงื่อนไขแพ้:** หาก `(Gyro > Threshold)` **OR** `(Pi_Model == Motion_Detected)` → เปลี่ยนเป็น `GAME_OVER`
   * *ไม่ใช้เสียง/ไมค์ในการตัดสิน*

5. **`WIN` / `GAME_OVER` (จบเกม)**
   * **กรณี `GAME_OVER` (แพ้):**
     * ส่งคำสั่ง MQTT ไปยัง ESP2 ให้ **Red LED = กะพริบต่อเนื่อง (BLINKING)** เพื่อให้ผู้เล่นรู้ตัวทันทีว่าแพ้
     * หน้าเว็บแสดงสถานะ `GAME OVER` แบบนิ่ง **(ไม่ต้องกะพริบบนหน้าเว็บ)** พร้อมระบุสาเหตุที่แพ้ (เช่น ขยับมือจาก Gyro หรือ ขยับตัวจากกล้อง) และเล่นเสียงเอฟเฟกต์
   * **กรณี `WIN` (ชนะ):**
     * สั่ง **Red LED = ดับ (OFF)** และหน้าเว็บแสดงผลชนะพร้อมเล่นเสียงฉลอง
   * **การทำงานร่วมหลังจบเกม:**
     * หยุดบันทึกข้อมูลรอบนั้นลง Time-Series DB และสรุปสถิติ (เวลาที่ใช้, Max BPM, ผลแพ้/ชนะ)
     * ส่งสถิติไปเรียก **AI Service** เพื่อสร้างคำพากย์/คำแซวผู้เล่น แล้วนำมาแสดงบนหน้าเว็บ

---

## 3. หน้าเว็บที่แสดงบนคอมพิวเตอร์ (Web Dashboard)

ออกแบบให้ครบทั้ง **Real-time Data**, **Historical Data** และตัวเลขสำหรับทำ **Evaluation** (หน้าเว็บไม่ต้องมีเอฟเฟกต์กะพริบตอนแพ้)

**แบ่งงาน:** เว็บของทีมทำส่วนที่ต้องโต้ตอบหรือแสดงสถานะเกม (Game Display, ปุ่มควบคุม, วิดีโอ, AI Commentator, Device Status) ส่วนกราฟจาก InfluxDB ทั้งหมดทำใน **Grafana** แล้วฝังด้วย iframe (คอลัมน์ "ทำด้วย")

| โซนบนหน้าเว็บ | สิ่งที่ต้องแสดง | แหล่งข้อมูล | ทำด้วย |
| :--- | :--- | :--- | :--- |
| **1. Game Display (Real-time)** | • แถบสถานะเกมชัดเจน (`IDLE` / `GREEN LIGHT` / `RED LIGHT` / `WIN` / `GAME OVER` แบบนิ่งไม่กะพริบ) + นาฬิกานับถอยหลัง<br>• **Live Video Stream** พร้อมกรอบ AI จับการขยับ และตัวเลข **FPS** แบบ Real-time<br>• **Distance Progress Bar** แสดงระยะห่างจากเส้นชัย | Game Engine,<br>Raspberry Pi (Camera, Ultrasonic) | เว็บของทีม |
| **2. Player Telemetry (Real-time)** | • ตัวเลขและกราฟเส้น **Heartbeat (BPM)** แบบ Real-time<br>• **เกจวัดความนิ่ง (Gyro Stability Meter)** แสดงระดับความสั่นของข้อมือเทียบกับขีดอันตราย | ESP1 (Heartbeat, Gyro) ผ่าน InfluxDB | Grafana (Time series + Gauge panel) |
| **3. Historical & AI Insights** | • **กราฟย้อนหลัง (Time-Series Chart):** เลือกดูกราฟ BPM และ Gyro ของรอบที่ผ่านมาได้<br>• **Match History / Leaderboard:** ตารางประวัติผู้เล่น (แพ้/ชนะ, เวลา, Max BPM)<br>• **AI Commentator Box:** ข้อความวิเคราะห์/คำพากย์จาก AI Service หลังจบเกม | Time-Series DB (InfluxDB),<br>AI Service API | กราฟ + ตาราง: Grafana<br>AI Box: เว็บของทีม |
| **4. Control & System Status** | • ปุ่ม `Start` / `Reset` / `Calibrate` และสวิตช์สลับโหมด Servo (**Auto** vs **Joystick**)<br>• **Device Status:** ไฟสถานะ Online/Offline ของ ESP1, ESP2, Pi และ Broker | Computer (MQTT Broker) | เว็บของทีม |

**ตั้งค่า Grafana ให้ฝังได้ (`grafana.ini`):**
* `[security] allow_embedding = true`
* `[auth.anonymous] enabled = true` และ `org_role = Viewer` (ไม่ต้องล็อกอินตอนเปิดหน้าเว็บ)
* `[dashboards] min_refresh_interval = 1s` (ค่าเริ่มต้นคือ 5s ช้าไปสำหรับกราฟ Real-time)
* ฝังทีละ panel ด้วยลิงก์ `/d-solo/...` และส่ง `&from=` / `&to=` ใน URL เพื่อเลือกช่วงเวลาของแต่ละรอบ

---

## 4. Edge Cases และแนวทางรับมือ

### ด้าน Network และจังหวะเวลา
* **Wi-Fi / MQTT Delay:** คำสั่งไฟแดงไปถึง ESP1 และ Pi ช้ากว่าหน้าเว็บ 100–300ms ทำให้ผู้เล่นโดนจับแพ้ทั้งที่หยุดทัน
  * *วิธีแก้:* บังคับใช้ **`Grace Period`** (0.5–1.0 วินาที) เสมอหลังไฟเปลี่ยนเป็นแดง ก่อนเปิดรับสัญญาณจับแพ้
* **อุปกรณ์หลุดกลางเกม (Disconnect):** แบต ESP1 หมดหรือ Wi-Fi หลุด
  * *วิธีแก้:* ตั้งค่า **MQTT Last Will and Testament (LWT)** ถ้าอุปกรณ์หลุดให้หน้าเว็บแจ้งเตือนและ Pause เกมทันที

### ด้านฮาร์ดแวร์และเซนเซอร์
* **ไฟ Red LED กะพริบแล้วบอร์ดค้าง (Blocking Delay):** หากใช้ `delay()` สั่งกะพริบไฟตอน `GAME_OVER` บน ESP2 จะทำให้บอร์ดรับคำสั่ง MQTT `Reset` ไม่ได้หรือหลุดจาก Broker
  * *วิธีแก้:* เขียนโค้ดกะพริบไฟแบบ **Non-blocking โดยใช้ `millis()`** เท่านั้น เพื่อให้ `client.loop()` ของ MQTT ทำงานได้ตลอดเวลา
* **มือสั่นตามธรรมชาติ (Gyro Noise):** ยืนนิ่งๆ ค่า Gyro ก็ไม่เป็น 0
  * *วิธีแก้:* ตั้ง **Deadzone / Threshold** และใช้ Moving Average กรองสัญญาณรบกวน
* **Heartbeat เพี้ยนตอนขยับตัว (Motion Artifact):** เซนเซอร์ชีพจรแบบแสงจะอ่านค่ามั่วเมื่อวิ่งหรือแกว่งแขน
  * *วิธีแก้:* ทำ Outlier Filter รับเฉพาะค่าในช่วง 45–180 BPM หากหลุดจากช่วงนี้ให้คงค่าเดิมไว้ก่อนและไม่บันทึกลง DB
* **Ultrasonic ยิงหลุดตัวผู้เล่น:** ผู้เล่นเดินเบี่ยงออกข้าง ทำให้คลื่นไปวัดกำแพงแทน
  * *วิธีแก้:* ใช้ Ultrasonic แค่แสดง Progress Bar คร่าวๆ เท่านั้น **ห้ามใช้ตัดสินการขยับตอนไฟแดง**
* **Ultrasonic บน Pi: ไฟเกินและค่าแกว่ง:** ขา Echo ของ HC-SR04 ส่งไฟ 5V ซึ่งเกินที่ GPIO รับได้ (3.3V) และ Linux บน Pi จับเวลาไม่แม่นเท่าไมโครคอนโทรลเลอร์
  * *วิธีแก้:* ใส่ **Voltage Divider** ที่ขา Echo (เช่น 1kΩ/2kΩ) ใช้ `gpiozero.DistanceSensor` หรือ `pigpio` และทำ Moving Average ก่อน Publish
* **MG90S Servo ดึงไฟกระชาก (Brownout):** Servo 2 ตัวหมุนพร้อมกันทำให้ ESP2 รีเซ็ต
  * *วิธีแก้:* **แยกแหล่งจ่ายไฟ 5V ให้ Servo ต่างหาก** (เช่น Adapter 5V 2A) แล้วต่อสาย GND ร่วมกับ ESP2
* **Joystick สั่งหมุนเกินองศา:** ดันค้างไว้จนเฟือง Servo รูด
  * *วิธีแก้:* ล็อกมุม (Clamp) ในโค้ด ESP2 ให้อยู่ในช่วง `15° - 165°`

### ด้านกติกา (Gameplay)
* **พุ่งแตะเส้นชัยตอนไฟแดงพอดี:** ผู้เล่นเอื้อมมือไปแตะ Touch Module จังหวะเปลี่ยนไฟ
  * *วิธีแก้:* กำหนด Priority ใน Game Engine ให้ชัด — แตะโดนช่วง `GREEN_LIGHT` หรือ `GRACE_PERIOD` = **ชนะ** / แตะโดนช่วง `RED_LIGHT` = **แพ้**

### ด้าน AI, Database และ Evaluation
* **AI Service (Cloud API) ช้าหรือล่ม:** เน็ตหลุดหรือ API ตอบช้าตอนจบเกมทำให้หน้าเว็บค้าง
  * *วิธีแก้:* ตั้ง Timeout ไว้ไม่เกิน 3–5 วินาที และทำ **Fallback Message** (ข้อความสำเร็จรูปในเครื่อง) แสดงแทนหากเรียก API ไม่สำเร็จ
* **FPS บน Raspberry Pi ตก:** รันโมเดลหนักเกินไปจนภาพกระตุกและจับการขยับไม่ทัน
  * *วิธีแก้:* ลดความละเอียดภาพก่อนส่งเข้าโมเดล (เช่น `320x240` หรือ `640x480`) ใช้โมเดลเบา (เช่น MediaPipe Pose Lite หรือ YOLOv8n) และเก็บค่า FPS, Accuracy รวมถึงภาพเคสที่จับได้/จับพลาด ไว้ทำ Evaluation ในสไลด์
* **Grafana ไม่ขึ้นใน iframe:** เบราว์เซอร์บล็อกหรือขึ้นหน้าล็อกอิน
  * *วิธีแก้:* เปิด `allow_embedding` และ anonymous viewer ตามหัวข้อ 3 และทดสอบเปิดหน้าเว็บจากเครื่องอื่นก่อนวันเดโม

---

## 5. สรุปความสอดคล้องกับเกณฑ์วิชา (2110575 Checklist)

* **Sensors ≥ 5 ชนิด:** 6 ชนิด ได้แก่ Heartbeat, Gyro, Ultrasonic, Touch, Joystick, Camera
* **I2C ≥ 1 ตัว:** MPU6050 (และ MAX30102 ถ้าใช้รุ่น I2C)
* **Sensor Node ≥ 2 ตัว:** ESP1 (Heartbeat + Gyro), ESP2 (Joystick)
* **Gateway ≥ 2 ตัว:** Raspberry Pi (Camera + Ultrasonic + Touch) ยังครบแม้ไม่นับกล้อง
* **Actuator ≥ 1 ตัว:** Pan-Tilt Servo 2 ตัว + Red LED
* **Application & AI:** Camera Stream + Keypoint/Motion Model บน Pi (ส่งผลให้เกิด `GAME_OVER`) + AI Service API (สรุปผล/พากย์หลังจบเกม) + เก็บสถิติ FPS/Accuracy และเคสสำเร็จ/พลาด
* **System Implementation:** MQTT ≥ 3 Topics + On-Premise Server + Time-Series DB (InfluxDB) + Dashboard (เว็บของทีม + Grafana, Real-time และ Historical Data)
