/*
 * KOKOWA Red Light Green Light — ESP32 เส้นชัย (finish node)
 *
 * Board   : ESP32-S3-N16R8 (บอร์ดเดียวกับ wrist)  ใช้ ESP32 รุ่นอื่นได้ แค่เปลี่ยนเลขขาด้านล่าง
 * Sensors : Ultrasonic HC-SR04  (TRIG -> GPIO5, ECHO -> GPIO6 ผ่านตัวแบ่งแรงดัน)
 *           ปุ่ม/เซนเซอร์สัมผัสที่เส้นชัย (TTP223 หรือ push button -> GPIO7)
 *
 * Publish : rlgl/gateway/distance   10 Hz ตอนเล่น, 1 Hz ตอนว่าง
 *                                   {distance_cm, valid, round_id}
 *           rlgl/gateway/finish     ทุกครั้งที่แตะ (กันเด้ง 500 ms)
 *                                   {touched=1, round_id, state, elapsed_s, first, press_count}
 *                                   elapsed_s = เวลาตั้งแต่ไฟเขียวแรกของรอบจนถึงตอนแตะ (จับเวลาใน interrupt)
 *           rlgl/status/finish-01   online/offline (retain + LWT)
 * Subscribe: rlgl/game/state        เอา round_id / state มาใส่ข้อความ และเริ่มจับเวลาตอนไฟเขียวแรกของรอบ
 *
 * ตาม docs/mqtt-contract.md: JSON ชั้นเดียว, จริง/เท็จ = 1/0, มี device_id / seq / ts (epoch ms, ยังไม่ sync = null)
 * หมายเหตุ: PubSubClient ส่งได้แค่ QoS 0 ถ้าแตะตอน MQTT หลุด จะเก็บไว้แล้วส่งทันทีที่ต่อได้ (ts / elapsed_s ยังเป็นของตอนแตะจริง)
 *
 * ต่อสาย
 *   HC-SR04  VCC -> 5V (VBUS/5V ของบอร์ด), GND -> GND, TRIG -> GPIO5
 *            ECHO ออก 5V ห้ามต่อเข้า ESP32 ตรง ๆ ต้องผ่านตัวแบ่งแรงดัน:
 *            ECHO --[1kΩ]--+--> GPIO6
 *                          |
 *                        [2kΩ]
 *                          |
 *                         GND
 *            (ถ้าเป็นรุ่น HC-SR04P / RCWL-1601 ที่ใช้ 3.3V ได้ ต่อ VCC 3.3V แล้วต่อ ECHO ตรงได้)
 *   TTP223   VCC -> 3.3V, GND -> GND, I/O -> GPIO7   (แตะ = HIGH)  ตั้ง TOUCH_ACTIVE_HIGH = true
 *   ปุ่มกด    ขาหนึ่ง -> GPIO7, อีกขา -> GND          (กด = LOW)   ตั้ง TOUCH_ACTIVE_HIGH = false
 *
 * Library : PubSubClient (by Nick O'Leary) — ติดตั้งจาก Library Manager
 * Arduino : Board "ESP32S3 Dev Module", USB CDC On Boot "Enabled" (ถ้าเสียบพอร์ต USB ไม่ใช่ COM)
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <time.h>
#include <sys/time.h>
#include <string.h>

// ===================== CONFIG (แก้ตรงนี้) =====================
const char* WIFI_SSID = "YOUR_WIFI_2.4GHz";
const char* WIFI_PASS = "YOUR_PASSWORD";
const char* MQTT_HOST = "172.20.10.3";      // IP ของ Pi (broker) ดูบน Pi ด้วย: hostname -I
const uint16_t MQTT_PORT = 1883;
const char* DEVICE_ID = "finish-01";
const char* NTP_SERVER = "pool.ntp.org";    // ถ้า hotspot ไม่มีเน็ต ts จะเป็น null (backend ใช้เวลาตอนรับแทน)

// ---- ขา (ESP32-S3) ----
const int PIN_TRIG  = 5;
const int PIN_ECHO  = 6;    // ผ่านตัวแบ่งแรงดัน 1k/2k
const int PIN_TOUCH = 7;
#ifdef RGB_BUILTIN
const int PIN_LED = RGB_BUILTIN;
#else
const int PIN_LED = 2;
#endif

const bool  TOUCH_ACTIVE_HIGH = true;   // TTP223 = true, ปุ่มกดลง GND = false
const uint32_t TOUCH_DEBOUNCE_MS = 500; // แตะซ้ำภายในเวลานี้ไม่นับ

const float DIST_MIN_CM = 2.0;          // HC-SR04 วัดได้ประมาณ 2-400 cm
const float DIST_MAX_CM = 400.0;

#define DEBUG_SERIAL 1  // 1 = พิมพ์ระยะ/การแตะลง Serial Monitor แม้ยังไม่ต่อ MQTT
// ==============================================================

// ---- timing ----
const uint32_t PING_MS          = 60;    // ยิงคลื่นทุก 60 ms (HC-SR04 ต้องเว้นอย่างน้อย ~60 ms กันเสียงสะท้อนเก่า)
const uint32_t ECHO_TIMEOUT_US  = 30000; // ไม่มีเสียงสะท้อนใน 30 ms (~5 m) = วัดไม่ได้
const uint32_t DIST_PLAY_MS     = 100;   // ส่งระยะ 10 Hz ตอนเล่น
const uint32_t DIST_IDLE_MS     = 1000;  // 1 Hz ตอนว่าง
const int      MEDIAN_N         = 5;     // median 5 ค่าล่าสุด ตัดค่ากระโดด

WiFiClient   net;
PubSubClient mqtt(net);

char TOPIC_DIST[48], TOPIC_FINISH[48], TOPIC_STATUS[48];
const char* TOPIC_STATE = "rlgl/game/state";
char MSG_ONLINE[80], MSG_OFFLINE[80];

// ---- game state (จาก rlgl/game/state) ----
char gameState[24] = "IDLE";
char roundId[72]   = "";        // ว่าง = ไม่รู้ (ส่ง null)
uint32_t roundStartMs = 0;      // millis() ตอนไฟเขียวแรกของรอบ (0 = ยังไม่เริ่ม)
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
uint32_t lastTouchMs = 0;
uint32_t finishSeq = 0;
int      pressCount = 0;        // จำนวนครั้งที่แตะในรอบนี้

// แตะตอน MQTT หลุด → เก็บไว้ส่งทีหลัง (เก็บล่าสุด 4 ครั้ง)
const int PENDING_MAX = 4;
char  pendingMsg[PENDING_MAX][300];
int   pendingN = 0;

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

// round_id เป็น JSON: "r012" หรือ null
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

bool isPlaying() {
  return strcmp(gameState, "CALIBRATE") == 0 || strcmp(gameState, "GREEN_LIGHT") == 0 ||
         strcmp(gameState, "TRANSITION") == 0 || strcmp(gameState, "RED_LIGHT") == 0;
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
  if (active && !touchEdge) {
    touchEdgeMs = millis();
    touchEdge = true;
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
    Serial.printf("[DIST] %s  state=%s round=%s | WiFi=%s MQTT=%s\n",
                  valid ? String(cm, 1).c_str() : "--", gameState, roundId[0] ? roundId : "-",
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
  if (!touchEdge) return;
  uint32_t atMs = touchEdgeMs;                     // เวลาตอนแตะจริง (จาก interrupt)
  touchEdge = false;
  if (lastTouchMs != 0 && atMs - lastTouchMs < TOUCH_DEBOUNCE_MS) return;
  lastTouchMs = atMs;
  pressCount++;

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

  // ส่งไม่ได้ → เก็บไว้ (เต็มแล้วทิ้งอันเก่าสุด)
  if (pendingN == PENDING_MAX) {
    for (int i = 1; i < PENDING_MAX; i++) strcpy(pendingMsg[i - 1], pendingMsg[i]);
    pendingN--;
  }
  strncpy(pendingMsg[pendingN], msg, sizeof pendingMsg[0] - 1);
  pendingMsg[pendingN][sizeof pendingMsg[0] - 1] = '\0';
  pendingN++;
  Serial.println("[TOUCH] MQTT ยังไม่ต่อ เก็บไว้ส่งทีหลัง");
}

void flushPending() {
  int sent = 0;
  while (sent < pendingN && mqtt.publish(TOPIC_FINISH, pendingMsg[sent])) sent++;
  for (int i = sent; i < pendingN; i++) strcpy(pendingMsg[i - sent], pendingMsg[i]);
  pendingN -= sent;
  if (sent) Serial.printf("[TOUCH] ส่งการแตะที่ค้างไว้ %d ครั้ง\n", sent);
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
  if (strcmp(st, "GREEN_LIGHT") == 0 && !roundStarted) {   // เริ่มจับเวลาที่ไฟเขียวแรกของรอบ
    roundStartMs = millis();
    roundStarted = true;
  }
  if (strcmp(st, gameState) != 0 || newRound) {
    Serial.printf("[GAME] state %s -> %s  round=%s\n", gameState, st, roundId[0] ? roundId : "-");
  }
  strncpy(gameState, st, sizeof gameState - 1);
  gameState[sizeof gameState - 1] = '\0';
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
  publishDistance(nowMs);
  handleNetwork();
  updateLed();
}
