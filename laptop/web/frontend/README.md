# frontend: หน้าเว็บเกม

HTML/CSS/JavaScript ธรรมดา ไม่ต้อง build ไม่ต้องลง package backend เปิดหน้านี้ที่ http://localhost:8000/ (วิธีเปิดและตั้งค่าอยู่ใน [`../README.md`](../README.md))
หน้าตาตาม design ใน Figma (ไฟล์ IOT)

```
frontend/
├── index.html        โครงทุกหน้า (ซ่อน/แสดงตาม state)
├── css/style.css     สีและขนาดตาม design
├── fonts/            ฟอนต์ pixel (ยังไม่มีไฟล์ ดูด้านล่าง)
└── js/
    ├── app.js        ต่อ /ws, เรียก API, วาดแต่ละหน้า
    ├── logic.js      ส่วนคำนวณ (เวลา, เลือกหน้าจอ, ข้อความสาเหตุ, timeline) มีเทสต์ใน tests/frontend/
    ├── charts.js     กราฟชีพจรและ timeline บน canvas
    └── sprites.js    รูป pixel art (วาดจากตาราง ไม่ใช้ไฟล์รูป)
```

## หน้าที่มี

| แท็บ | หน้า | แสดงเมื่อ |
|---|---|---|
| LIVE GAME | ใส่ชื่อ + เลือกโหมด (RED LIGHT / SNIPER) | `IDLE` หรือยังไม่มีรอบ |
| | Calibrate | `CALIBRATE` |
| | ไฟเขียว / เหลือง / แดง | `GREEN_LIGHT`, `TRANSITION`, `RED_LIGHT` |
| | Sniper scope | `SNIPER` |
| | ผลรอบ (ชนะ / แพ้) | `WIN`, `GAME_OVER` จนกว่าจะกด PLAY AGAIN หรือรอบใหม่เริ่ม |
| SCOREBOARD | Overview (Top 7 แยกโหมด), Round History, รายละเอียดรอบ + วิดีโอ | กดเอง (จอผู้เล่นกดไม่ได้ระหว่างเล่น) |

## ข้อมูลที่ยังขึ้นกับคนทำ Pi

- ภาพสดใน calibrate และ Sniper ต้องตั้ง `RLGL_STREAM_URL` ถ้าไม่ตั้งหรือดึงไม่ได้ หน้าเว็บจะขึ้นกรอบว่างพร้อมข้อความ และลองต่อใหม่ทุก 4 วินาที
- ช่องในหน้า calibrate (baseline, % ความคืบหน้า), Sniper (lock-on, ชีวิต, dodges), สาเหตุแพ้ละเอียด (ส่วนร่างกาย, วินาทีในไฟแดง) ใช้ field ที่เสนอไว้ใน [`docs/mqtt-contract.md`](../../../docs/mqtt-contract.md) ถ้า Pi ไม่ส่ง ช่องนั้นจะเป็น `--`
- ยังไม่มีโครงร่างบนวิดีโอ replay และกราฟ motion score ในหน้ารายละเอียดรอบ (ต้องมีข้อมูลจุดร่างกายที่ส่งมาพร้อมคลิป)

## ฟอนต์

design ใช้ **Press Start 2P** และ **Pixelify Sans** ไฟล์อยู่ใน `fonts/` แล้ว (ดาวน์โหลดจาก repo `google/fonts` บน GitHub เมื่อ 26 พ.ย.) ใช้ได้โดยไม่ต้องต่อ internet
สัญญาอนุญาตเป็น SIL Open Font License (`fonts/OFL-*.txt`) ต้องเก็บไฟล์สัญญาไว้คู่กับฟอนต์เมื่อแจกต่อ ถ้าไม่มีไฟล์ฟอนต์ หน้าเว็บจะใช้ฟอนต์ของเครื่องแทน
