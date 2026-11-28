// ค่าที่ต่างกันในแต่ละที่ (Wi-Fi และ IP ของ broker) ไม่ขึ้น git
// วิธีใช้: คัดลอกไฟล์นี้ไปเป็น secrets.h ในโฟลเดอร์ของแต่ละ sketch (esp32/wrist/secrets.h, esp32/finish/secrets.h)
//          แล้วแก้ค่าข้างล่าง  secrets.h อยู่ใน .gitignore แล้ว ห้าม commit (repo เป็น public)
#pragma once

const char* WIFI_SSID = "YOUR_WIFI_2.4GHz";  // ESP32 ต่อได้แค่ 2.4 GHz (iPhone: เปิด Maximize Compatibility)
const char* WIFI_PASS = "YOUR_PASSWORD";
const char* MQTT_HOST = "172.20.10.2";       // IP ของ Pi (broker) ดูบน Pi ด้วย: hostname -I
