"""เปลี่ยน IP ของ broker (Pi) ทุกจุดบนโน้ตบุ๊กในครั้งเดียว

วิธีใช้ (ในโฟลเดอร์ laptop/infra):
    python set_broker_ip.py 172.20.10.2
    python set_broker_ip.py 172.20.10.2 --no-restart   # ไม่รีสตาร์ต Telegraf ให้

แก้ให้:
  - infra/.env           : MQTT_URL (Telegraf) และ RLGL_MQTT_HOST (backend เว็บ)
  - rlgl-dashboard/config.js : PI_HOST (WebSocket, วิดีโอ, captures)
แล้วรัน `docker compose up -d` เพื่อให้ Telegraf โหลดค่าใหม่

ที่สคริปต์นี้แก้ให้ไม่ได้: โค้ดบน ESP1/ESP2 (`#define MQTT_HOST`) ต้องแก้แล้วแฟลชเอง
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
CONFIG_JS = HERE.parent.parent / "rlgl-dashboard" / "config.js"


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
    if not CONFIG_JS.exists():
        sys.exit(f"ไม่พบ {CONFIG_JS}")

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
    ENV_FILE.write_bytes(env_text.encode("utf-8"))
    print(f"[แก้] .env  MQTT_URL: {old_url} -> tcp://{ip}:{args.port}")
    print(f"[แก้] .env  RLGL_MQTT_HOST: {old_host} -> {ip}")

    # config.js ของหน้าเว็บ
    js_text = CONFIG_JS.read_bytes().decode("utf-8")
    js_pattern = re.compile(r"const PI_HOST = '([^']*)';")
    match = js_pattern.search(js_text)
    if not match:
        sys.exit("ไม่พบบรรทัด const PI_HOST = '...'; ใน config.js (รูปแบบไฟล์เปลี่ยนไป)")
    CONFIG_JS.write_bytes(js_pattern.sub(f"const PI_HOST = '{ip}';", js_text, count=1).encode("utf-8"))
    print(f"[แก้] config.js  PI_HOST: {match.group(1)} -> {ip}")

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
    print(f"  - ESP1, ESP2: แก้ #define MQTT_HOST \"{ip}\" แล้วแฟลชใหม่")
    print("  - backend เว็บ (ถ้ากำลังรันอยู่): รีสตาร์ตเพื่อให้อ่าน RLGL_MQTT_HOST ใหม่")
    print("  - หน้าเว็บ: รีเฟรชเบราว์เซอร์")


if __name__ == "__main__":
    main()
