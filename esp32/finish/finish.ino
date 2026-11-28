/*
 * KOKOWA Red Light Green Light — ESP32 เส้นชัย + จอยสติ๊ก (finish node)
 *
 * Board   : ESP32-S3-N16R8 (บอร์ดเดียวกับ wrist)
 * Sensors : เซนเซอร์สัมผัสโลหะ KY-036 ที่เส้นชัย   DO -> GPIO5 (AO -> GPIO4 ไม่ได้ใช้ในโค้ด)
 *           Ultrasonic (ขา SDA/SCL ใช้แบบ GPIO)  ECHO (SDA) -> GPIO6, TRIG (SCL) -> GPIO7
 *           Joystick                             แกน Y -> GPIO1 (ADC1), ปุ่ม SW -> GPIO16
 *           Servo กล้อง (เกม 2)                  สายสีส้ม -> GPIO8
 *
 * Publish : rlgl/gateway/distance   10 Hz ตอนเล่น, 1 Hz ตอนว่าง   {distance_cm, valid, round_id}
 *           rlgl/gateway/finish     ทุกครั้งที่แตะ (กันเด้ง 500 ms)
 *                                   {touched=1, round_id, state, elapsed_s, first, press_count}
 *           rlgl/station/button     กดปุ่มจอย 1 ครั้ง = ส่ง 1 ครั้ง {pressed=1}
 *                                   game engine: calibrate / เริ่มเล่น / เกม 1 เปลี่ยนไฟ
 *           rlgl/station/camera     มุม servo ตอนเกม 2 (retain) {pan, tilt, facing}
 *           rlgl/status/finish-01   online/offline (retain + LWT)
 * Subscribe: rlgl/game/state        round_id / state, จับเวลาตั้งแต่เริ่มเล่น, เปิด servo เฉพาะ SNIPER
 *
 * ตาม docs/mqtt-contract.md: JSON ชั้นเดียว, จริง/เท็จ = 1/0, มี device_id / seq / ts (epoch ms, ยังไม่ sync = null)
 *
 * ต่อสาย
 *   KY-036   + -> 3V3 (ห้ามใช้ 5V), G -> GND, D0 -> GPIO5, A0 -> GPIO4 (ไม่ใช้)
 *            หมุน potentiometer บนโมดูลจนไฟ LED บนบอร์ดติดเฉพาะตอนแตะ
 *   Ultrasonic (RCWL-1601 / HC-SR04 รุ่นที่มีป้าย SDA/SCL) ต้องอยู่ในโหมด GPIO (ค่าเริ่มต้นของโมดูล)
 *            SCL = TRIG -> GPIO7, SDA = ECHO -> GPIO6
 *            ถ้าเลี้ยง 5V: ECHO ต้องผ่านตัวแบ่งแรงดัน (1kΩ ต่ออนุกรม, 2kΩ ลง GND) ก่อนเข้า GPIO6
 *            ถ้าเลี้ยง 3.3V ได้: ต่อ VCC 3V3 แล้วต่อ ECHO ตรงได้
 *   Joystick VCC -> 3V3, GND -> GND, VRy -> GPIO1, SW -> GPIO16 (VRx ไม่ใช้)
 *            ห้ามต่อแกนเข้า GPIO11-20 (ADC2) เพราะใช้ analogRead ไม่ได้ตอนเปิด Wi-Fi
 *   Servo    น้ำตาล/ดำ -> GND, แดง -> 5V (VBUS หรือแหล่งจ่ายแยก, GND ร่วมกับ ESP), ส้ม -> GPIO8
 *
 * Library : PubSubClient (Nick O'Leary), ESP32Servo (Kevin Harrington) — ติดตั้งจาก Library Manager
 * Arduino : Board "ESP32S3 Dev Module", USB CDC On Boot "Enabled" (ถ้าเสียบพอร์ต USB ไม่ใช่ COM)
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <time.h>
#include <sys/time.h>
#include <string.h>

// ===================== CONFIG (แก้ตรงนี้) =====================
const char* WIFI_SSID = "PAI";
const char* WIFI_PASS = "YOUR_PASSWORD";    // ใส่รหัสจริงตอนแฟลช แต่อย่า commit รหัสจริง (repo เป็น public)
const char* MQTT_HOST = "172.20.10.2";      // IP ของ Pi (broker) ดูบน Pi ด้วย: hostname -I
const uint16_t MQTT_PORT = 1883;
const char* DEVICE_ID = "finish-01";
const char* NTP_SERVER = "pool.ntp.org";    // ถ้า hotspot ไม่มีเน็ต ts จะเป็น null (backend ใช้เวลาตอนรับแทน)

// ---- ขา (ESP32-S3) ----
const int PIN_TOUCH    = 5;    // KY-036 DO
const int PIN_TOUCH_AO = 4;    // KY-036 AO (ไม่ได้ใช้)
const int PIN_ECHO     = 6;    // ultrasonic ขา SDA = ECHO
const int PIN_TRIG     = 7;    // ultrasonic ขา SCL = TRIG
const int PIN_SERVO    = 8;    // servo สายสีส้ม
const int PIN_JOY_Y    = 1;    // joystick แกน Y: ต้องเป็นขา ADC1 (GPIO1-10) ย้ายสายจาก GPIO15 มาที่นี่
const int PIN_JOY_SW   = 16;   // joystick ปุ่มกด (กด = LOW)
#ifdef RGB_BUILTIN
const int PIN_LED = RGB_BUILTIN;
#else
const int PIN_LED = 2;
#endif

const bool  TOUCH_ACTIVE_HIGH = true;   // KY-036 / TTP223 = true, ปุ่มกดลง GND = false
const uint32_t TOUCH_DEBOUNCE_MS = 500; // แตะซ้ำภายในเวลานี้ไม่นับ
const uint32_t TOUCH_RELEASE_MS  = 150; // ไม่เจอสัญญาณแตะนานเท่านี้ = ปล่อยมือแล้ว (ต้อง > 20 ms ของไฟ 50 Hz)
const uint32_t PENDING_MAX_AGE_MS = 5000; // การแตะที่ส่งไม่ทันเกินเวลานี้ทิ้ง (กันชนะผิดรอบ)

const float DIST_MIN_CM = 2.0;          // ultrasonic วัดได้ประมาณ 2-400 cm
const float DIST_MAX_CM = 400.0;

const uint32_t BUTTON_DEBOUNCE_MS = 300; // กดปุ่มจอยซ้ำภายในเวลานี้ไม่นับ
const int  JOY_DEADZONE   = 300;         // ค่าห่างจากตรงกลางน้อยกว่านี้ = ปล่อยมือ (0-4095)
const int  JOY_STEP_DIV   = 400;         // ยิ่งน้อยยิ่งหมุนเร็ว (องศาต่อรอบ = ค่าที่ดัน / ค่านี้)
const bool JOY_INVERT     = false;       // true = ดันขึ้นแล้วหมุนกลับทาง
const int  SERVO_MIN_DEG  = 0;
const int  SERVO_MAX_DEG  = 180;
const int  SERVO_HOME_DEG = 90;          // มุมตอนเริ่ม / นอกเกม 2

#define DEBUG_SERIAL 1  // 1 = พิมพ์ระยะ/การแตะ/จอยลง Serial Monitor
// ==============================================================

// ---- timing ----
const uint32_t PING_MS          = 60;    // ยิงคลื่นทุก 60 ms (ต้องเว้นอย่างน้อย ~60 ms กันเสียงสะท้อนเก่า)
const uint32_t ECHO_TIMEOUT_US  = 30000; // ไม่มีเสียงสะท้อนใน 30 ms (~5 m) = วัดไม่ได้
const uint32_t DIST_PLAY_MS     = 100;   // ส่งระยะ 10 Hz ตอนเล่น
const uint32_t DIST_IDLE_MS     = 1000;  // 1 Hz ตอนว่าง
const int      MEDIAN_N         = 5;     // median 5 ค่าล่าสุด ตัดค่ากระโดด
const uint32_t SERVO_EVERY_MS   = 20;    // อัปเดต servo 50 ครั้ง/วินาที
const uint32_t CAMERA_PUB_MS    = 200;   // ส่งมุมกล้องให้หน้าเว็บไม่เกิน 5 ครั้ง/วินาที

WiFiClient   net;
PubSubClient mqtt(net);
Servo        camServo;

char TOPIC_DIST[48], TOPIC_FINISH[48], TOPIC_STATUS[48];
const char* TOPIC_STATE  = "rlgl/game/state";
const char* TOPIC_BUTTON = "rlgl/station/button";
const char* TOPIC_CAMERA = "rlgl/station/camera";
char MSG_ONLINE[80], MSG_OFFLINE[80];

// ---- game state (จาก rlgl/game/state) ----
char gameState[24] = "IDLE";
char roundId[72]   = "";        // ว่าง = ไม่รู้ (ส่ง null)
uint32_t roundStartMs = 0;      // millis() ตอนเริ่มเล่น (ไฟเขียวแรก หรือ SNIPER)
bool roundStarted = false;

// ---- ultrasonic (วัดด้วย interrupt ไม่ block loop) ----
volatile uint32_t echoRiseUs = 0;
volatile uint32_t echoWidthUs = 0;
volatile bool     echoReady = false;
uint32_t lastPingMs = 0, pingSentUs = 0;
bool     waitingEcho = false;
float    distBuf[MEDIAN_N];
int      distCount = 0, distIdx = 0;
bool     lastDistValid = false;
uint32_t distSeq = 0, lastDistPubMs = 0;

// ---- touch (จับเวลาใน interrupt ให้แม่นแม้ loop ติดตอนต่อ MQTT) ----
volatile uint32_t touchEdgeMs = 0;
volatile bool     touchEdge = false;
volatile uint32_t touchLastActiveMs = 0;   // เจอสัญญาณแตะครั้งล่าสุด (KY-036 กะพริบ 50 Hz ระหว่างแตะ)
volatile bool     touchHeld = false;       // กำลังแตะค้างอยู่ → ไม่นับเป็นการแตะใหม่
uint32_t lastTouchMs = 0;
uint32_t finishSeq = 0;
int      pressCount = 0;        // จำนวนครั้งที่แตะระหว่างเล่นในรอบนี้

// แตะตอน MQTT หลุด → เก็บไว้ส่งทีหลัง (เก็บล่าสุด 4 ครั้ง, เก่าเกิน PENDING_MAX_AGE_MS ทิ้ง)
const int PENDING_MAX = 4;
char     pendingMsg[PENDING_MAX][300];
uint32_t pendingAtMs[PENDING_MAX];
int      pendingN = 0;

// ---- joystick + servo ----
bool     swPrev = HIGH;
uint32_t lastSwMs = 0, buttonSeq = 0;
int      joyCenter = 2048;      // วัดตอนเปิดเครื่อง (ปล่อยจอยไว้ตรงกลาง)
int      servoAngle = SERVO_HOME_DEG, lastPubAngle = -1;
uint32_t lastServoMs = 0, lastCameraPubMs = 0, cameraSeq = 0;

uint32_t lastMqttTry = 0;

// =============================================================
//  Utilities
// =============================================================
bool timeSynced() { return time(nullptr) > 1700000000; }

// ts (epoch ms) ของเวลา millis() ที่กำหนด; ยังไม่ sync เวลา = "null"
void formatTsAt(uint32_t atMs, char* buf, size_t len) {
  if (!timeSynced()) { snprintf(buf, len, "null"); return; }
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  long long nowMs = (long long)tv.tv_sec * 1000LL + tv.tv_usec / 1000;
  long long ms = nowMs - (long long)(millis() - atMs);
  snprintf(buf, len, "%lld", ms);
}

// round_id เป็น JSON: "1795842406" หรือ null
void formatRoundId(char* buf, size_t len) {
  if (roundId[0] == '\0') snprintf(buf, len, "null");
  else snprintf(buf, len, "\"%s\"", roundId);
}

// ดึงค่าของ key จาก JSON ชั้นเดียวแบบง่าย (รองรับ "key": "str", "key":123, "key": null)
// คืน true ถ้าเจอ key; out = "" ถ้าเป็น null
bool jsonGet(const char* json, const char* key, char* out, size_t len) {
  char pat[40];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char* p = strstr(json, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') return false;
  p++;
  while (*p == ' ' || *p == '\t') p++;
  size_t n = 0;
  if (*p == '"') {
    p++;
    while (*p && *p != '"' && n + 1 < len) out[n++] = *p++;
  } else if (strncmp(p, "null", 4) == 0) {
    n = 0;
  } else {
    while (*p && *p != ',' && *p != '}' && *p != ' ' && n + 1 < len) out[n++] = *p++;
  }
  out[n] = '\0';
  return true;
}

bool stateIs(const char* s) { return strcmp(gameState, s) == 0; }

// ช่วงที่ส่งระยะถี่ (10 Hz)
bool isPlaying() {
  return stateIs("CALIBRATE") || stateIs("COUNTDOWN") || stateIs("GREEN_LIGHT") ||
         stateIs("TRANSITION") || stateIs("RED_LIGHT") || stateIs("SNIPER");
}

// ช่วงที่การแตะเส้นชัยมีผล (นับ first / press_count)
bool isRunning() {
  return stateIs("GREEN_LIGHT") || stateIs("TRANSITION") || stateIs("RED_LIGHT") || stateIs("SNIPER");
}

// =============================================================
//  Interrupts
// =============================================================
void IRAM_ATTR onEcho() {
  uint32_t now = micros();
  if (digitalRead(PIN_ECHO)) {
    echoRiseUs = now;
  } else if (echoRiseUs != 0) {
    echoWidthUs = now - echoRiseUs;
    echoRiseUs = 0;
    echoReady = true;
  }
}

void IRAM_ATTR onTouch() {
  bool active = digitalRead(PIN_TOUCH) == (TOUCH_ACTIVE_HIGH ? HIGH : LOW);
  if (!active) return;
  uint32_t now = millis();
  touchLastActiveMs = now;
  if (!touchHeld) {            // เริ่มแตะใหม่ (ไม่ใช่สัญญาณกะพริบระหว่างแตะค้าง)
    touchHeld = true;
    if (!touchEdge) {
      touchEdgeMs = now;
      touchEdge = true;
    }
  }
}

// =============================================================
//  Ultrasonic
// =============================================================
void sendPing() {
  echoReady = false;
  echoRiseUs = 0;
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  pingSentUs = micros();
  waitingEcho = true;
}

float medianDist() {
  float tmp[MEDIAN_N];
  for (int i = 0; i < distCount; i++) tmp[i] = distBuf[i];
  for (int i = 1; i < distCount; i++) {          // insertion sort (มีแค่ 5 ค่า)
    float v = tmp[i];
    int j = i - 1;
    while (j >= 0 && tmp[j] > v) { tmp[j + 1] = tmp[j]; j--; }
    tmp[j + 1] = v;
  }
  return tmp[distCount / 2];
}

void handleUltrasonic(uint32_t nowMs) {
  if (waitingEcho) {
    if (echoReady) {
      waitingEcho = false;
      float cm = echoWidthUs / 58.0f;              // เสียง 343 m/s ไป-กลับ
      if (cm >= DIST_MIN_CM && cm <= DIST_MAX_CM) {
        distBuf[distIdx] = cm;
        distIdx = (distIdx + 1) % MEDIAN_N;
        if (distCount < MEDIAN_N) distCount++;
        lastDistValid = true;
      } else {
        lastDistValid = false;
      }
    } else if (micros() - pingSentUs > ECHO_TIMEOUT_US) {
      waitingEcho = false;                         // ไม่มีเสียงสะท้อน (ไกลเกิน / หันผิดทาง)
      lastDistValid = false;
    }
  }
  if (!waitingEcho && nowMs - lastPingMs >= PING_MS) {
    lastPingMs = nowMs;
    sendPing();
  }
}

void publishDistance(uint32_t nowMs) {
  uint32_t every = isPlaying() ? DIST_PLAY_MS : DIST_IDLE_MS;
  if (nowMs - lastDistPubMs < every) return;
  lastDistPubMs = nowMs;
  bool valid = lastDistValid && distCount > 0;
  float cm = valid ? medianDist() : 0;

#if DEBUG_SERIAL
  static uint32_t lastDbg = 0;
  if (nowMs - lastDbg >= 500) {
    lastDbg = nowMs;
    Serial.printf("[DIST] %s  joyY=%d servo=%d  state=%s round=%s | WiFi=%s MQTT=%s\n",
                  valid ? String(cm, 1).c_str() : "--", analogRead(PIN_JOY_Y), servoAngle,
                  gameState, roundId[0] ? roundId : "-",
                  WiFi.status() == WL_CONNECTED ? "OK" : "--", mqtt.connected() ? "OK" : "--");
  }
#endif
  if (!mqtt.connected()) return;
  char ts[24], rid[80], msg[256];
  formatTsAt(nowMs, ts, sizeof ts);
  formatRoundId(rid, sizeof rid);
  if (valid) {
    snprintf(msg, sizeof msg,
             "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,\"round_id\":%s,\"distance_cm\":%.1f,\"valid\":1}",
             DEVICE_ID, (unsigned long)++distSeq, ts, rid, cm);
  } else {  // วัดไม่ได้: ไม่ใส่ distance_cm (กราฟจะไม่ตกไปที่ 0)
    snprintf(msg, sizeof msg,
             "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,\"round_id\":%s,\"valid\":0}",
             DEVICE_ID, (unsigned long)++distSeq, ts, rid);
  }
  mqtt.publish(TOPIC_DIST, msg);                   // QoS 0
}

// =============================================================
//  Touch / finish
// =============================================================
void handleTouch() {
  if (touchHeld && millis() - touchLastActiveMs > TOUCH_RELEASE_MS) touchHeld = false;  // ปล่อยมือแล้ว
  if (!touchEdge) return;
  uint32_t atMs = touchEdgeMs;                     // เวลาตอนแตะจริง (จาก interrupt)
  touchEdge = false;
  if (lastTouchMs != 0 && atMs - lastTouchMs < TOUCH_DEBOUNCE_MS) return;
  lastTouchMs = atMs;
  if (isRunning()) pressCount++;                   // นับเฉพาะตอนเล่น (แตะตอนรอไม่ทำให้ first เพี้ยน)

  char ts[24], rid[80], elapsed[20], msg[300];
  formatTsAt(atMs, ts, sizeof ts);
  formatRoundId(rid, sizeof rid);
  if (roundStarted) snprintf(elapsed, sizeof elapsed, "%.3f", (atMs - roundStartMs) / 1000.0f);
  else snprintf(elapsed, sizeof elapsed, "null");
  snprintf(msg, sizeof msg,
           "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,\"round_id\":%s,\"touched\":1,"
           "\"state\":\"%s\",\"elapsed_s\":%s,\"first\":%d,\"press_count\":%d}",
           DEVICE_ID, (unsigned long)++finishSeq, ts, rid, gameState, elapsed,
           pressCount == 1 ? 1 : 0, pressCount);

  Serial.printf("[TOUCH] แตะเส้นชัย! state=%s round=%s elapsed=%s s (ครั้งที่ %d)\n",
                gameState, roundId[0] ? roundId : "-", elapsed, pressCount);
  if (mqtt.connected() && mqtt.publish(TOPIC_FINISH, msg)) return;

  // ไม่รู้รอบ (ยังไม่ได้รับ state) → ไม่เก็บ เพราะส่งทีหลังแล้ว game engine จะนับเป็นของรอบไหนก็ได้
  if (roundId[0] == '\0') {
    Serial.println("[TOUCH] MQTT ยังไม่ต่อ และยังไม่รู้รอบ ไม่เก็บการแตะนี้");
    return;
  }
  // ส่งไม่ได้ → เก็บไว้ (เต็มแล้วทิ้งอันเก่าสุด)
  if (pendingN == PENDING_MAX) {
    for (int i = 1; i < PENDING_MAX; i++) {
      strcpy(pendingMsg[i - 1], pendingMsg[i]);
      pendingAtMs[i - 1] = pendingAtMs[i];
    }
    pendingN--;
  }
  strncpy(pendingMsg[pendingN], msg, sizeof pendingMsg[0] - 1);
  pendingMsg[pendingN][sizeof pendingMsg[0] - 1] = '\0';
  pendingAtMs[pendingN] = atMs;
  pendingN++;
  Serial.println("[TOUCH] MQTT ยังไม่ต่อ เก็บไว้ส่งทีหลัง");
}

void flushPending() {
  int sent = 0, dropped = 0;
  for (int i = 0; i < pendingN; i++) {
    if (millis() - pendingAtMs[i] > PENDING_MAX_AGE_MS) { dropped++; continue; }   // เก่าเกิน ทิ้ง
    if (mqtt.publish(TOPIC_FINISH, pendingMsg[i])) sent++;
  }
  pendingN = 0;
  if (sent || dropped) Serial.printf("[TOUCH] ส่งการแตะที่ค้างไว้ %d ครั้ง, ทิ้ง %d ครั้ง (เก่าเกิน)\n", sent, dropped);
}

// =============================================================
//  Joystick: ปุ่ม = START / เปลี่ยนไฟ, แกน Y = หมุนกล้อง (เกม 2)
// =============================================================
void handleJoystickButton(uint32_t nowMs) {
  bool sw = digitalRead(PIN_JOY_SW);               // กด = LOW (INPUT_PULLUP)
  if (swPrev == HIGH && sw == LOW && nowMs - lastSwMs > BUTTON_DEBOUNCE_MS) {
    lastSwMs = nowMs;
    Serial.printf("[JOY] กดปุ่ม (state=%s)\n", gameState);
    if (mqtt.connected()) {
      char ts[24], rid[80], msg[200];
      formatTsAt(nowMs, ts, sizeof ts);
      formatRoundId(rid, sizeof rid);
      snprintf(msg, sizeof msg, "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,\"round_id\":%s,\"pressed\":1}",
               DEVICE_ID, (unsigned long)++buttonSeq, ts, rid);
      mqtt.publish(TOPIC_BUTTON, msg);             // กด 1 ครั้ง = ส่ง 1 ครั้ง
    } else {
      Serial.println("[JOY] MQTT ยังไม่ต่อ ปุ่มนี้ไม่ถูกส่ง");   // ไม่เก็บไว้ส่งทีหลัง กันไฟเปลี่ยนเองภายหลัง
    }
  }
  swPrev = sw;
}

void publishCamera(uint32_t nowMs, bool force) {
  if (!mqtt.connected()) return;
  if (!force && (servoAngle == lastPubAngle || nowMs - lastCameraPubMs < CAMERA_PUB_MS)) return;
  lastCameraPubMs = nowMs;
  lastPubAngle = servoAngle;
  char ts[24], msg[200];
  formatTsAt(nowMs, ts, sizeof ts);
  snprintf(msg, sizeof msg, "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,\"pan\":%d,\"tilt\":90,\"facing\":\"player\"}",
           DEVICE_ID, (unsigned long)++cameraSeq, ts, servoAngle);
  mqtt.publish(TOPIC_CAMERA, msg, true);           // retain: หน้าเว็บที่เพิ่งเปิดเห็นมุมล่าสุด
}

void handleServo(uint32_t nowMs) {
  if (nowMs - lastServoMs < SERVO_EVERY_MS) return;
  lastServoMs = nowMs;
  if (!stateIs("SNIPER")) {                        // นอกเกม 2: กลับมุมตั้งต้นแล้วหยุด
    if (servoAngle != SERVO_HOME_DEG) {
      servoAngle = SERVO_HOME_DEG;
      camServo.write(servoAngle);
    }
    return;
  }
  int d = analogRead(PIN_JOY_Y) - joyCenter;       // 0-4095
  if (JOY_INVERT) d = -d;
  if (abs(d) < JOY_DEADZONE) return;               // ปล่อยมือ: กล้องไม่ไหล
  int next = constrain(servoAngle + d / JOY_STEP_DIV, SERVO_MIN_DEG, SERVO_MAX_DEG);   // ดันมาก = หมุนเร็ว
  if (next != servoAngle) {
    servoAngle = next;
    camServo.write(servoAngle);
  }
  publishCamera(nowMs, false);
}

// =============================================================
//  MQTT: รับ state ของเกม
// =============================================================
void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  if (strcmp(topic, TOPIC_STATE) != 0) return;
  char json[400];
  unsigned int n = len < sizeof json - 1 ? len : sizeof json - 1;
  memcpy(json, payload, n);
  json[n] = '\0';

  char st[24], rid[72];
  if (!jsonGet(json, "state", st, sizeof st) || st[0] == '\0') return;
  bool hasRid = jsonGet(json, "round_id", rid, sizeof rid);
  if (!hasRid) rid[0] = '\0';

  bool newRound = strcmp(rid, roundId) != 0;
  if (newRound) {                                  // รอบใหม่ → รีเซ็ตการจับเวลาและนับการแตะ
    strncpy(roundId, rid, sizeof roundId - 1);
    roundId[sizeof roundId - 1] = '\0';
    roundStarted = false;
    pressCount = 0;
  }
  if (strcmp(st, "IDLE") == 0) {
    roundStarted = false;
    pressCount = 0;
  }
  // เริ่มจับเวลาตอนเริ่มเล่นจริง: ไฟเขียวแรก (เกม 1) หรือ SNIPER (เกม 2)
  if ((strcmp(st, "GREEN_LIGHT") == 0 || strcmp(st, "SNIPER") == 0) && !roundStarted) {
    roundStartMs = millis();
    roundStarted = true;
  }
  if (strcmp(st, gameState) != 0 || newRound) {
    Serial.printf("[GAME] state %s -> %s  round=%s\n", gameState, st, roundId[0] ? roundId : "-");
  }
  bool enteringSniper = strcmp(st, "SNIPER") == 0 && strcmp(gameState, "SNIPER") != 0;
  strncpy(gameState, st, sizeof gameState - 1);
  gameState[sizeof gameState - 1] = '\0';
  if (enteringSniper) publishCamera(millis(), true);   // บอกหน้าเว็บมุมเริ่มต้น
}

// =============================================================
//  Wi-Fi / MQTT
// =============================================================
bool mqttConnect() {
  Serial.printf("[MQTT] connecting to %s:%u ... ", MQTT_HOST, MQTT_PORT);
  bool ok = mqtt.connect(DEVICE_ID, nullptr, nullptr, TOPIC_STATUS, 1, true, MSG_OFFLINE);
  if (ok) {
    mqtt.publish(TOPIC_STATUS, MSG_ONLINE, true);
    mqtt.subscribe(TOPIC_STATE, 1);                // retain → ได้ state ปัจจุบันทันที
    Serial.println("OK");
    flushPending();
  } else {
    Serial.printf("fail (state %d)\n", mqtt.state());
  }
  return ok;
}

void handleNetwork() {
  if (WiFi.status() != WL_CONNECTED) return;     // Wi-Fi จะ auto-reconnect เอง
  if (mqtt.connected()) { mqtt.loop(); return; }
  if (millis() - lastMqttTry > 2000) {           // ลองต่อใหม่ทุก 2 วินาที
    lastMqttTry = millis();
    mqttConnect();
  }
}

void updateLed() {
  if (millis() - lastTouchMs < 300 && lastTouchMs != 0) digitalWrite(PIN_LED, LOW);  // ดับวูบ = เพิ่งแตะ
  else if (mqtt.connected()) digitalWrite(PIN_LED, HIGH);                          // ติดค้าง = ต่อ broker แล้ว
  else digitalWrite(PIN_LED, (millis() / 250) % 2);                                // กะพริบ = ยังไม่ต่อ
}

// =============================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(PIN_LED, OUTPUT);

  snprintf(TOPIC_DIST,   sizeof TOPIC_DIST,   "rlgl/gateway/distance");
  snprintf(TOPIC_FINISH, sizeof TOPIC_FINISH, "rlgl/gateway/finish");
  snprintf(TOPIC_STATUS, sizeof TOPIC_STATUS, "rlgl/status/%s", DEVICE_ID);
  snprintf(MSG_ONLINE,  sizeof MSG_ONLINE,  "{\"device_id\":\"%s\",\"status\":\"online\"}",  DEVICE_ID);
  snprintf(MSG_OFFLINE, sizeof MSG_OFFLINE, "{\"device_id\":\"%s\",\"status\":\"offline\"}", DEVICE_ID);

  // Sensors
  pinMode(PIN_TRIG, OUTPUT);
  digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_ECHO, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_ECHO), onEcho, CHANGE);
  pinMode(PIN_TOUCH, TOUCH_ACTIVE_HIGH ? INPUT_PULLDOWN : INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_TOUCH), onTouch, CHANGE);

  // Joystick: วัดค่าตรงกลางตอนเปิดเครื่อง (อย่าแตะจอยตอนเสียบไฟ)
  pinMode(PIN_JOY_SW, INPUT_PULLUP);
  analogReadResolution(12);
  long sum = 0;
  for (int i = 0; i < 32; i++) { sum += analogRead(PIN_JOY_Y); delay(2); }
  joyCenter = sum / 32;
  Serial.printf("[JOY] center Y = %d\n", joyCenter);

  // Servo
  ESP32PWM::allocateTimer(0);
  camServo.setPeriodHertz(50);
  camServo.attach(PIN_SERVO, 500, 2400);
  camServo.write(SERVO_HOME_DEG);

  // Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                          // ปิด power save -> latency ต่ำลง
  WiFi.setAutoReconnect(true);

  Serial.println("[WiFi] scanning...");
  int nNet = WiFi.scanNetworks();
  bool found = false;
  for (int i = 0; i < nNet; i++) {
    bool match = (WiFi.SSID(i) == String(WIFI_SSID));
    if (match) found = true;
    Serial.printf("  %s \"%s\"  RSSI=%d dBm  ch=%d\n",
                  match ? "->" : "  ", WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i));
  }
  Serial.printf("[WiFi] SSID \"%s\" %s\n", WIFI_SSID,
                found ? "FOUND" : "NOT FOUND (ชื่อผิด / เป็น 5 GHz / อยู่ไกลเกิน)");
  WiFi.scanDelete();

  // บอกเหตุผลเมื่อต่อ Wi-Fi ไม่ได้ (15/202 = รหัสผิด, 201 = หา SSID ไม่เจอ, 203/205 = hotspot ไม่รับเพิ่ม)
  WiFi.onEvent([](WiFiEvent_t e, WiFiEventInfo_t info) {
    Serial.printf("[WiFi] หลุด/ต่อไม่ได้ reason=%d\n", info.wifi_sta_disconnected.reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[WiFi] connecting");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print(" OK, IP = ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.printf(" not yet, status=%d (จะลองต่อเองเรื่อย ๆ)\n", (int)WiFi.status());
  }

  configTime(0, 0, NTP_SERVER);                  // ts เป็น epoch UTC ms

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setKeepAlive(5);                          // หลุดแล้ว broker รู้ภายใน ~7 วินาที
  mqtt.setSocketTimeout(2);
  mqtt.setBufferSize(512);
}

void loop() {
  uint32_t nowMs = millis();
  handleUltrasonic(nowMs);   // ไม่ block: ยิงคลื่นแล้วรอผลจาก interrupt
  handleTouch();             // เวลาแตะถูกจดใน interrupt แล้ว
  handleJoystickButton(nowMs);
  handleServo(nowMs);
  publishDistance(nowMs);
  handleNetwork();
  updateLed();
}
