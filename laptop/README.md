# ฝั่ง laptop: InfluxDB, Telegraf, Grafana และตัวจำลอง

```
อุปกรณ์หรือตัวจำลอง ──MQTT──► broker ──► Telegraf ──► InfluxDB ──► Grafana
```

## สิ่งที่ต้องมี

- Docker Desktop (เปิดไว้)
- Python 3.10 ขึ้นไป และ `pip install "paho-mqtt>=2"` สำหรับตัวจำลอง

## เริ่มใช้งาน

```powershell
cd laptop/infra
```

```powershell
python make_env.py
```

```powershell
docker compose up -d
```

ครั้งแรก Docker จะดาวน์โหลด image `influxdb:2.7`, `telegraf:1.35`, `grafana/grafana:11.6.0` และ `eclipse-mosquitto:2`

```powershell
python ../sim/simulate.py --rounds 3 --fake-backend
```

เปิด Grafana ที่ http://localhost:3000 บัญชีผู้ดูแลอยู่ใน `laptop/infra/.env`

ตัวจำลองเล่นได้ทั้งสองโหมด: `--mode sniper --scenario win|hits|timeout` (ค่าเริ่มต้น `--mode rlgl`)

`--fake-backend` ให้ตัวจำลองส่ง `rlgl/game/summary` แทน ใช้**เฉพาะตอนไม่ได้เปิด backend** ถ้าเปิด backend แล้ว (ดู [`web/README.md`](web/README.md)) ให้ใช้ `--backend http://127.0.0.1:8000` แทน

## ตั้งค่า (`infra/.env`)

| ค่า | ใช้ทำอะไร |
|---|---|
| `MQTT_URL` | ที่อยู่ broker ที่ Telegraf จะ subscribe |
| `COMPOSE_PROFILES=devbroker` | เปิด broker ชั่วคราวบน laptop สำหรับพัฒนา **ตอนรวมระบบให้ลบบรรทัดนี้** แล้วตั้ง `MQTT_URL=tcp://<IP ของ Pi>:1883` (broker เดียวของระบบอยู่บน Pi) |
| `MQTT_USER`, `MQTT_PASSWORD` | ต้องตรงกับ broker ที่ต่อ |
| `BIND_IP` | `127.0.0.1` = ใช้ได้เฉพาะเครื่องนี้ เปลี่ยนเป็น `0.0.0.0` เมื่อต้องให้เครื่องอื่นในวงเปิด Grafana ได้ (หรือเมื่อใช้ broker บน laptop ตามแผนสำรองและต้องให้ Pi/ESP32 ต่อเข้ามา) |

ห้าม commit ไฟล์ `.env` (repo นี้เป็น public)

## ถ้า IP ของ Pi เปลี่ยน

รันคำสั่งเดียวในโฟลเดอร์ `laptop/infra` (ดู IP ใหม่บน Pi ด้วย `hostname -I`):

```powershell
python set_broker_ip.py <IP ใหม่>
```

สคริปต์แก้ `MQTT_URL`, `RLGL_MQTT_HOST` และ `RLGL_STREAM_URL` ใน `.env` แล้วรีสตาร์ต Telegraf ให้ ส่วน ESP ต้องแก้ `MQTT_HOST` แล้วแฟลชเอง

## แก้ dashboard

แก้ `infra/grafana/make_dashboard.py` แล้วรันใหม่ ไฟล์ `dashboards/rlgl.json` ถูกสร้างจากสคริปต์นี้
เส้นเกณฑ์ในกราฟ (0.3 g และ 0.05) เป็นค่าตัวอย่าง ต้องปรับตามผล calibrate

## ชื่อ topic และ field

ดู [`docs/mqtt-contract.md`](../docs/mqtt-contract.md) เป็น**ฉบับร่าง** ที่ทีมยังต้องยืนยัน
บางข้อยังไม่ตรงกับ `docs/ARCHITECTURE.md` เช่น เรื่อง LED และตำแหน่ง game engine

## สถานะ

- ทดสอบด้วย**ข้อมูลจำลอง**บน Windows + Docker Desktop (25 พ.ย. 2026):
  - จำนวนข้อความ imu, distance, vision, camera, state, result ใน InfluxDB ตรงกับที่ส่ง
  - ชีพจรนอกช่วง 45-180 ไม่ถูกบันทึก
  - query ของ panel ทั้ง 15 ได้ข้อมูล และ dashboard แสดงผลสดได้
  - ดู dashboard ได้โดยไม่ล็อกอิน แต่แก้ไขไม่ได้
- **ยังไม่ได้ทดสอบ:** ข้อมูลจาก ESP32 และ Pi จริง, การต่อจากเครื่องอื่นในวง LAN, broker บน Pi, ความเร็ว query เมื่อข้อมูลสะสมมาก
- ตาราง "คลิปวิดีโอ" ทดสอบด้วยข้อความตัวอย่าง ยังไม่มีไฟล์วิดีโอจริง
- `rlgl/game/summary` ในการทดสอบ ตัวจำลองส่งแทน backend

## เว็บเกม

backend และหน้าเว็บอยู่ใน [`web/`](web/README.md) เปิดทั้งหมดด้วย `web\run.cmd`

## Game engine

ตัวคุมเกมและตัดสินแพ้ชนะอยู่ใน [`engine/`](engine/README.md) เปิดด้วย `engine\run.cmd` แล้วกด Enter เพื่อเริ่มรอบ

## ถ้า Telegraf ต่อ Pi ไม่ได้ (`no route to host`)

วงภายในของ Docker ต้องไม่ทับวงที่ Pi อยู่ `docker-compose.yml` ตั้งไว้ที่ `192.168.250.0/24` เพื่อไม่ให้ทับ hotspot iPhone (`172.20.10.x`) ถ้าวง Wi-Fi ที่ใช้เป็น `192.168.250.x` พอดี ให้เปลี่ยน subnet ใน `docker-compose.yml` แล้วรัน `docker compose down` ตามด้วย `docker compose up -d` (ข้อมูลไม่หาย)

## ล้างข้อมูลทดสอบ (ครั้งเดียว ก่อนเริ่มเก็บข้อมูลจริง)

ข้อมูลจากตัวจำลองที่ส่งผ่าน broker ปนอยู่ใน InfluxDB และแยกจากของจริงไม่ได้ (ใช้ชื่ออุปกรณ์เดียวกัน) ล้างครั้งเดียวก่อนวันเก็บข้อมูลจริง หลังจากนั้นทดสอบด้วย `web\run.cmd -Sim` ซึ่งไม่ส่งอะไรเข้า InfluxDB

ดับเบิลคลิก `clear_test_data.cmd` หรือรัน:

```powershell
.\clear_test_data.cmd -DryRun
```

```powershell
.\clear_test_data.cmd
```

- แบบแรกแสดงอย่างเดียว แบบที่สองแสดงแล้ว**ถามทีละส่วน** (Enter เฉย ๆ = ไม่ลบ) **ลบแล้วกู้คืนไม่ได้**
- ส่วนที่ล้างได้: `influx` (ทุกค่าใน bucket จนถึงตอนที่รัน), `players` (`web\data\rlgl.db`), `dev` (ข้อมูลของ `-Sim`), `clips` (`web\clips`) เลือกเฉพาะบางส่วนด้วย `-Only influx` หรือ `-Only players,dev`
- ต้องเปิด Docker stack อยู่จึงล้าง InfluxDB ได้ ต้องปิด backend ก่อนจึงลบฐานข้อมูลได้ (สคริปต์จะข้ามและบอกถ้ายังเปิดอยู่)
- คลิปใน Supabase ไม่ถูกลบ ลบเองในหน้า Storage ของ Supabase

## หยุดและเริ่มใหม่

```powershell
docker compose stop
```

```powershell
docker compose start
```

ข้อมูลเก็บใน Docker volume คำสั่ง `docker compose down -v` จะ**ลบข้อมูลทั้งหมด**
