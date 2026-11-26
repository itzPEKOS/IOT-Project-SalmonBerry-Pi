// ===== ตั้งค่าที่ต้องแก้ให้ตรงกับเครื่องจริง (แก้ที่ไฟล์นี้ที่เดียว) =====
// IP ของ Pi (ตั้งให้คงที่ที่ router) ใช้เป็นทั้ง broker และแหล่งวิดีโอ
const PI_HOST = '192.168.1.50';

const CONFIG = {
  // Broker เดียวของระบบอยู่บน Pi (ต้องเปิด listener แบบ websockets ที่ port 9001 ดู README)
  // ถ้าใช้แผนสำรอง (broker บนโน้ตบุ๊ก) ให้เปลี่ยนเป็น `ws://${location.hostname || 'localhost'}:9001`
  MQTT_WS_URL: `ws://${PI_HOST}:9001`,

  // วิดีโอสตรีมจาก Pi (MJPEG ผ่าน HTTP) ให้ Pi วาดกรอบ AI ลงบนเฟรมก่อนส่งมา
  PI_VIDEO_URL: `http://${PI_HOST}:8080/stream.mjpg`,
  // หน้ารวมภาพเคสที่จับได้/จับพลาด (ปุ่ม s บน Pi) ใช้ในแผง Evaluation
  PI_CAPTURES_URL: `http://${PI_HOST}:8080/captures/`,

  // Grafana (ต้องเปิด allow_embedding + anonymous viewer ดู spec หัวข้อ 3)
  GRAFANA: {
    BASE: 'http://localhost:3000',
    UID: 'rlgl',          // uid ของ dashboard ใน Grafana
    SLUG: 'rlgl',         // ชื่อใน URL ของ dashboard
    PANELS: {             // panelId ของแต่ละ panel (ดูจาก Panel > Share > Link)
      bpm: 1,             // Stat/Time series BPM real-time
      gyro: 2,            // Gauge ความนิ่งของข้อมือ
      historyBpm: 3,      // Time series BPM ย้อนหลัง
      historyGyro: 4,     // Time series Gyro ย้อนหลัง
      leaderboard: 5      // Table ประวัติผู้เล่น
    }
  },

  START_DISTANCE_CM: 300, // ระยะจาก Ultrasonic ตอนเริ่มเกม (ใช้คำนวณ progress bar)
  FINISH_DISTANCE_CM: 10, // ระยะที่ถือว่าถึงเส้นชัย

  // ===== MQTT topics (ทุกตัวต้องอยู่ใต้ rlgl/ ตามข้อตกลงของทีม) =====
  TOPICS: {
    state:       'rlgl/game/state',        // Game Engine -> เว็บ (retained)
    calibration: 'rlgl/game/calibration',  // Game Engine -> เว็บ
    rounds:      'rlgl/game/rounds',       // Game Engine -> เว็บ (retained, รายการรอบทั้งหมด)
    distance:    'rlgl/pi/distance',       // Pi -> เว็บ
    motion:      'rlgl/pi/motion',         // Pi -> เว็บ
    eval:        'rlgl/pi/eval',           // Pi/สคริปต์ทดสอบ -> เว็บ (retained)
    statusPrefix:'rlgl/status/',           // + esp1 | esp2 | pi  (LWT, retained)
    commentary:  'rlgl/ai/commentary',     // Game Engine -> เว็บ
    cmdGame:     'rlgl/cmd/game',          // เว็บ -> Game Engine {cmd: start|reset|calibrate}
    cmdServo:    'rlgl/cmd/servo_mode'     // เว็บ -> Game Engine {mode: auto|joystick}
  },

  DEVICES: ['esp1', 'esp2', 'pi'],
  DEMO: new URLSearchParams(location.search).has('demo') // เปิดด้วย ?demo=1 เพื่อลองโดยไม่ต้องมี broker
};
