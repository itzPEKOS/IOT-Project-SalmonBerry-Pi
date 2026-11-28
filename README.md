# Red Light, Green Light (IoT)

เกม Red Light, Green Light ด้วย IoT: ผู้เล่นเดินตอนไฟเขียว นิ่งตอนไฟแดง ระบบจับการขยับจากกล้อง (MediaPipe บน Raspberry Pi) และ IMU ที่ข้อมือ
งานกลุ่มวิชา 2110575 IoT Systems and Digital Solutions (2026/1)

## โครงสร้าง

| โฟลเดอร์ | รันที่ไหน | มีอะไร |
|---|---|---|
| [`esp32/`](esp32) | ESP32 | `wrist/` ESP ข้อมือ (MPU6050 + ชีพจร KY-039), `finish/` ESP เส้นชัย (touch + ultrasonic HC-SR04) |
| [`pi/`](pi) | Raspberry Pi | `pi_client.py` (กล้อง + MediaPipe → MQTT, ภาพสด MJPEG, อัดคลิป), broker Mosquitto (`setup_mosquitto.sh`) |
| [`laptop/`](laptop/README.md) | laptop | [`engine/`](laptop/engine/README.md) game engine, [`web/`](laptop/web/README.md) backend + หน้าเว็บ, [`infra/`](laptop/README.md) InfluxDB, Telegraf, Grafana (Docker) |
| [`docs/`](docs) | | [`ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`spec.md`](docs/spec.md), [`mqtt-contract.md`](docs/mqtt-contract.md) (topic และ field ทั้งหมด) |
| [`tools/`](tools) | Pi / laptop | วัดความแม่นของกล้อง: `pi_detect.py --record` บันทึก keypoint แล้ว `eval_motion.py` ประเมินผล |

## เปิดทั้งระบบ (ตามลำดับ)

ทุกเครื่องต่อ Wi-Fi วงเดียวกัน (ตอนนี้ใช้ hotspot, Pi = `172.20.10.2`)

1. **Pi: broker** (ครั้งแรกครั้งเดียว: `bash pi/setup_mosquitto.sh` หลังจากนั้น Mosquitto เปิดเองทุกครั้งที่บูต)
2. **Pi: กล้อง**

   ```bash
   cd pi
   ```

   ```bash
   python3 pi_client.py --backend http://<IP laptop>:8000
   ```

   ภาพสดอยู่ที่ `http://<IP Pi>:8000/stream` ส่วน `--backend` ใช้ส่งคลิปของรอบให้ laptop
3. **ESP:** แก้ `WIFI_SSID`, `WIFI_PASS`, `MQTT_HOST` (IP ของ Pi) ในส่วน CONFIG บนสุดของ `esp32/wrist/wrist.ino` และ `esp32/finish/finish.ino` แล้วแฟลช **อย่า commit รหัส Wi-Fi จริง**
4. **laptop: Docker** (InfluxDB, Telegraf, Grafana) ในโฟลเดอร์ `laptop\infra`:

   ```powershell
   docker compose up -d
   ```

5. **laptop: backend + หน้าเว็บ:** `laptop\web\run.cmd` แล้วเปิด http://localhost:8000
6. **laptop: game engine:** `laptop\engine\run.cmd` แล้วกด Enter (หรือปุ่ม joystick) เพื่อ calibrate และเริ่มเล่น

ถ้า IP ของ Pi เปลี่ยน รันในโฟลเดอร์ `laptop\infra`:

```powershell
python set_broker_ip.py <IP ใหม่>
```

แล้วแก้ `MQTT_HOST` ใน `.ino` ของ ESP แต่ละตัว และแฟลชใหม่

## ห้าม commit (repo เป็น public)

`laptop/infra/.env`, รหัส Wi-Fi จริงใน `.ino` ของ ESP, ไฟล์โมเดล (`*.pt`, `*.task`), รูปและคลิปที่มีหน้าคน (`captures/`, `replays/`, `laptop/web/clips/`) และฐานข้อมูลผู้เล่น (`laptop/web/data/`) ทั้งหมดยกเว้นรหัส Wi-Fi อยู่ใน `.gitignore` แล้ว
