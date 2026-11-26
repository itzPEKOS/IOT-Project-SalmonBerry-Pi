#!/usr/bin/env bash
# ติดตั้งและตั้งค่า Mosquitto บน Raspberry Pi (Pi OS 64-bit) ให้เป็น broker เดียวของระบบ RLGL
# วิธีใช้: bash pi/setup_mosquitto.sh   (ใช้ sudo เองในสคริปต์)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_SRC="$DIR/mosquitto/rlgl.conf"
CONF_DST="/etc/mosquitto/conf.d/rlgl.conf"

[ -f "$CONF_SRC" ] || { echo "ไม่พบ $CONF_SRC"; exit 1; }

echo "== ติดตั้ง mosquitto และ mosquitto-clients =="
sudo apt-get update
sudo apt-get install -y mosquitto mosquitto-clients

echo "== คัดลอก config ไปที่ $CONF_DST =="
sudo install -m 644 "$CONF_SRC" "$CONF_DST"

echo "== เปิดใช้งานและรีสตาร์ต =="
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sleep 1

if ! systemctl is-active --quiet mosquitto; then
  echo "Mosquitto ไม่ขึ้น ดู error ด้วย: sudo journalctl -u mosquitto -n 30 --no-pager"
  exit 1
fi

echo "== port ที่เปิดอยู่ (ต้องเห็น 1883 และ 9001 ที่ 0.0.0.0) =="
ss -ltn | grep -E ':(1883|9001)\b' || echo "ไม่เห็น port 1883/9001"

echo "== ทดสอบ pub/sub ในเครื่อง =="
RESULT="$(timeout 3 mosquitto_sub -h localhost -t 'rlgl/test' -C 1 & sleep 0.5; mosquitto_pub -h localhost -t 'rlgl/test' -m 'ok'; wait)"
echo "ได้รับ: ${RESULT:-<ไม่ได้รับข้อความ>}"

echo
echo "IP ของ Pi (ใช้เป็น MQTT_HOST ใน ESP และ MQTT_URL ใน laptop/infra/.env):"
hostname -I
echo "แนะนำให้ reserve IP นี้ที่ router เพื่อไม่ให้เปลี่ยนวันเดโม"
