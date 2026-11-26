"""สร้างไฟล์ .env ที่มีรหัสผ่านแบบสุ่มสำหรับเครื่องนี้ (ไม่เขียนทับไฟล์เดิม)"""
import pathlib
import secrets
import sys

here = pathlib.Path(__file__).resolve().parent
target = here / ".env"
if target.exists():
    sys.exit(".env มีอยู่แล้ว ไม่ได้แก้ไข")

template = (here / ".env.example").read_text(encoding="utf-8")
lines = []
for line in template.splitlines():
    if line.endswith("=CHANGE_ME"):
        # ใช้เฉพาะ 0-9 a-f: ค่าที่ขึ้นต้นด้วย "-" ทำให้ InfluxDB อ่านเป็น option และตั้งค่าไม่สำเร็จ
        line = line[: -len("CHANGE_ME")] + secrets.token_hex(24)
    lines.append(line)
target.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("สร้าง .env แล้ว")
