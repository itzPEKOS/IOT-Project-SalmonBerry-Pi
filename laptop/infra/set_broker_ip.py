"""เปลี่ยน IP ของ broker (Pi) ทุกจุดบนโน้ตบุ๊กในครั้งเดียว

วิธีใช้ (ในโฟลเดอร์ laptop/infra):
    python set_broker_ip.py 172.20.10.2
    python set_broker_ip.py 172.20.10.2 --no-restart   # ไม่รีสตาร์ต Telegraf ให้

แก้ให้ใน infra/.env:
  - MQTT_URL         (Telegraf)
  - RLGL_MQTT_HOST   (backend เว็บ และ game engine)
  - RLGL_STREAM_URL  (ภาพสดจากกล้อง Pi: เปลี่ยนแค่ IP, port และ path คงเดิม)
แล้วรัน `docker compose up -d` เพื่อให้ Telegraf โหลดค่าใหม่

ที่สคริปต์นี้แก้ให้ไม่ได้: ESP แต่ละตัว (MQTT_HOST ใน esp32/<ชื่อ>/<ชื่อ>.ino) ต้องแก้แล้วแฟลชเอง
"""
import argparse
import ipaddress
import pathlib
import re
import shutil
import socket
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


def set_env_key(text: str, key: str, value: str) -> tuple[str, str]:
    """แก้ KEY=... ในข้อความของ .env (ถ้าไม่มีให้เพิ่มท้ายไฟล์) คืน (ข้อความใหม่, ค่าเดิม)"""
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$", re.MULTILINE)
    match = pattern.search(text)
    if match:
        return pattern.sub(lambda _: f"{key}={value}", text, count=1), match.group(1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{key}={value}\n", "(ยังไม่มี)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ip", help="IP ใหม่ของ Pi (ดูบน Pi ด้วย: hostname -I)")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--no-restart", action="store_true", help="ไม่รัน docker compose up -d")
    args = parser.parse_args()

    try:
        ip = str(ipaddress.IPv4Address(args.ip))
    except ValueError:
        sys.exit(f"'{args.ip}' ไม่ใช่ IPv4 ที่ถูกต้อง (ตัวอย่าง: 172.20.10.2)")

    if not ENV_FILE.exists():
        sys.exit(f"ไม่พบ {ENV_FILE} รัน python make_env.py ก่อน")

    # เช็กก่อนว่าต่อ broker ได้จริง ถ้าไม่ได้ให้เตือนแต่ยังแก้ให้ (เผื่อ Pi ยังไม่เปิด)
    try:
        with socket.create_connection((ip, args.port), timeout=3):
            print(f"[ok] ต่อ {ip}:{args.port} ได้")
    except OSError as err:
        print(f"[เตือน] ต่อ {ip}:{args.port} ไม่ได้ ({err}) ยังแก้ไฟล์ให้ตามที่สั่ง ตรวจว่า Pi เปิดและ Mosquitto ทำงานอยู่")

    # .env
    env_text = ENV_FILE.read_bytes().decode("utf-8")
    env_text, old_url = set_env_key(env_text, "MQTT_URL", f"tcp://{ip}:{args.port}")
    env_text, old_host = set_env_key(env_text, "RLGL_MQTT_HOST", ip)
    stream = re.search(r"^RLGL_STREAM_URL=(.*)$", env_text, re.MULTILINE)
    old_stream = stream.group(1).strip() if stream else ""
    new_stream = (re.sub(r"^(https?://)[^/:]+", rf"\g<1>{ip}", old_stream) if old_stream
                  else f"http://{ip}:8000/stream")  # ค่าเริ่มต้นของ pi_client.py
    env_text, _ = set_env_key(env_text, "RLGL_STREAM_URL", new_stream)
    ENV_FILE.write_bytes(env_text.encode("utf-8"))
    print(f"[แก้] .env  RLGL_STREAM_URL: {old_stream or '(ยังไม่มี)'} -> {new_stream}")
    print(f"[แก้] .env  MQTT_URL: {old_url} -> tcp://{ip}:{args.port}")
    print(f"[แก้] .env  RLGL_MQTT_HOST: {old_host} -> {ip}")

    # Telegraf
    if args.no_restart:
        print("[ข้าม] ไม่ได้รีสตาร์ต Telegraf (--no-restart) รัน `docker compose up -d` เอง")
    elif shutil.which("docker") is None:
        print("[ข้าม] ไม่พบคำสั่ง docker รัน `docker compose up -d` เอง")
    else:
        print("[รัน] docker compose up -d ...")
        result = subprocess.run(["docker", "compose", "up", "-d"], cwd=HERE)
        if result.returncode != 0:
            sys.exit("docker compose ล้มเหลว ดู error ด้านบน")

    print()
    print("ยังต้องทำเอง:")
    print(f"  - ESP ทุกตัว: แก้ MQTT_HOST = \"{ip}\" ใน esp32/<ชื่อ>/<ชื่อ>.ino แล้วแฟลชใหม่")
    print("  - backend เว็บ และ game engine (ถ้ากำลังรันอยู่): รีสตาร์ตเพื่อให้อ่านค่าใหม่")
    print("  - หน้าเว็บ: รีเฟรชเบราว์เซอร์")


if __name__ == "__main__":
    main()
