/*
 * KOKOWA Red Light Green Light — ESP32 #1 (wrist node, ติดตัวผู้เล่น)
 *
 * Board   : ESP32-S3-N16R8
 * Sensors : MPU-6050 (I2C: SDA GPIO8, SCL GPIO9)
 *           Heart rate optical (KY-039 / HW-502, analog S -> GPIO4)
 * Publish : rlgl/wrist/imu        20 Hz  {acc_rms, acc_peak, gyro_rms}
 *           rlgl/wrist/heartrate   1 Hz  {raw, signal, amp, bpm, valid, motion}
 *           rlgl/status/wrist-01   online/offline (retain + LWT)
 *
 * Library : PubSubClient (by Nick O'Leary) — ติดตั้งจาก Library Manager
 * Arduino : Board "ESP32S3 Dev Module", Flash Size 16MB, PSRAM "OPI PSRAM",
 *           USB CDC On Boot "Enabled" (ถ้าเสียบพอร์ต USB ไม่ใช่ COM)
 */

#include <WiFi.h>
#include <Wire.h>
#include <PubSubClient.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>

// ===================== CONFIG (แก้ตรงนี้) =====================
const char* WIFI_SSID = "YOUR_WIFI_2.4GHz";
const char* WIFI_PASS = "YOUR_PASSWORD";
const char* MQTT_HOST = "192.168.1.50";     // IP ของเครื่องที่รัน Mosquitto (ตอนนี้ = laptop, ทีหลังเปลี่ยนเป็น Pi)
const uint16_t MQTT_PORT = 1883;
const char* DEVICE_ID = "wrist-01";
const char* NTP_SERVER = "pool.ntp.org";    // ถ้า hotspot ไม่มีเน็ต ใส่ IP ของ Pi แทนได้ (ถ้า Pi เปิด NTP server)

// ---- ขาสำหรับ ESP32-S3-N16R8 ----
const int PIN_SDA = 8;      // I2C SDA -> MPU-6050 SDA
const int PIN_SCL = 9;      // I2C SCL -> MPU-6050 SCL
const int PIN_HR  = 4;      // ADC1_CH3 (บน S3 ต้องใช้ ADC1 = GPIO1-10 เพราะ ADC2 ใช้ไม่ได้ตอนเปิด Wi-Fi)
#ifdef RGB_BUILTIN
const int PIN_LED = RGB_BUILTIN;   // RGB LED บนบอร์ด (GPIO48) — digitalWrite จะติดเป็นสีขาว
#else
const int PIN_LED = 2;
#endif

const bool  HR_INVERT       = false;  // ถ้า BPM เพี้ยน/จับจังหวะผิด ลองสลับเป็น true
const float HR_MIN_AMP      = 4.0;    // (ADC counts) สัญญาณเล็กกว่านี้ = ไม่มีนิ้ว/สัญญาณรบกวน
const float MOTION_ACC_RMS  = 0.05;   // (g) ข้อมือขยับเกินนี้ -> ติดธง motion (BPM ไม่น่าเชื่อ)

#define PLOT_HR 0       // 1 = พิมพ์สัญญาณชีพจรออก Serial Plotter ไว้จูน (ปิดตอนใช้งานจริง)
#define DEBUG_SERIAL 1  // 1 = พิมพ์ค่า sensor ลง Serial Monitor (IMU 2 ครั้ง/วิ, HR 1 ครั้ง/วิ) แม้ยังไม่ต่อ MQTT
// ==============================================================

// ---- timing ----
const uint32_t SAMPLE_US      = 10000;  // 100 Hz sampling (ทั้ง IMU และ HR)
const int      IMU_WINDOW     = 5;      // 5 samples = 50 ms -> publish 20 Hz
const uint32_t HR_PUBLISH_MS  = 1000;   // 1 Hz

// ---- MPU-6050 ----
const uint8_t MPU_ADDR = 0x68;
const float ACC_LSB = 8192.0f;   // ±4 g
const float GYR_LSB = 65.5f;     // ±500 °/s

WiFiClient   net;
PubSubClient mqtt(net);

char TOPIC_IMU[48], TOPIC_HR[48], TOPIC_STATUS[48];
char MSG_ONLINE[80], MSG_OFFLINE[80];

// calibration (ทำตอนเปิดเครื่อง ต้องวางนิ่ง ~2 วินาที)
float gyroBiasX = 0, gyroBiasY = 0, gyroBiasZ = 0;
float gravityMag = 1.0f;

// IMU window
float  winAccSq = 0, winAccPeak = 0, winGyrSq = 0;
int    winN = 0;
uint32_t imuSeq = 0;
int    imuFailCount = 0;
uint32_t lastMotionMs = 0;

// heart rate
float hrBaseline = 0, hrSignal = 0, hrEnv = 0;
int   hrRaw = 0;
bool  hrAbove = false;
uint32_t lastBeatMs = 0;
uint32_t ibi[5] = {0};
int   ibiCount = 0, ibiIdx = 0;
float bpm = 0;
uint32_t hrSeq = 0;

uint32_t nextSampleUs = 0;
uint32_t lastHrPubMs  = 0;
uint32_t lastMqttTry  = 0;

// =============================================================
//  Utilities
// =============================================================
bool timeSynced() { return time(nullptr) > 1700000000; }

// เขียน ts (epoch ms) ลง buf; ถ้ายังไม่ sync เวลา ใส่ null ให้ Pi ประทับเวลาตอนรับแทน
void formatTs(char* buf, size_t len) {
  if (!timeSynced()) { snprintf(buf, len, "null"); return; }
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  long long ms = (long long)tv.tv_sec * 1000LL + tv.tv_usec / 1000;
  snprintf(buf, len, "%lld", ms);
}

// =============================================================
//  MPU-6050 (อ่าน register ตรง ไม่ต้องลง library เพิ่ม)
// =============================================================
bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool mpuInit() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x75);                              // WHO_AM_I
  if (Wire.endTransmission(false) != 0) return false;
  Wire.requestFrom(MPU_ADDR, (uint8_t)1);
  if (Wire.available()) {
    uint8_t who = Wire.read();
    Serial.printf("[MPU] WHO_AM_I = 0x%02X\n", who); // ของแท้ 0x68, บอร์ด clone อาจได้ 0x70/0x72 ก็ใช้ได้
  }
  bool ok = true;
  ok &= mpuWrite(0x6B, 0x01);   // PWR_MGMT_1: ปลุก + ใช้ clock จาก gyro X
  delay(50);
  ok &= mpuWrite(0x1A, 0x03);   // CONFIG: DLPF ~44 Hz (ตัด noise สั่น)
  ok &= mpuWrite(0x19, 0x09);   // SMPLRT_DIV: 1 kHz / (1+9) = 100 Hz
  ok &= mpuWrite(0x1B, 0x08);   // GYRO_CONFIG: ±500 °/s
  ok &= mpuWrite(0x1C, 0x08);   // ACCEL_CONFIG: ±4 g
  return ok;
}

bool mpuRead(float& ax, float& ay, float& az, float& gx, float& gy, float& gz) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);                              // ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)14) != 14) return false;
  int16_t v[7];
  for (int i = 0; i < 7; i++) {
    uint8_t hi = Wire.read();
    uint8_t lo = Wire.read();
    v[i] = (int16_t)((hi << 8) | lo);
  }
  ax = v[0] / ACC_LSB;  ay = v[1] / ACC_LSB;  az = v[2] / ACC_LSB;
  // v[3] = temperature (ไม่ใช้)
  gx = v[4] / GYR_LSB;  gy = v[5] / GYR_LSB;  gz = v[6] / GYR_LSB;
  return true;
}

// วางนิ่ง ~2 วินาที: หา gyro bias และขนาดแรงโน้มถ่วงจริงของเซนเซอร์ตัวนี้
void mpuCalibrate() {
  Serial.println("[MPU] Calibrating... อย่าขยับ 2 วินาที");
  const int N = 200;
  double sgx = 0, sgy = 0, sgz = 0, sg = 0;
  int got = 0;
  for (int i = 0; i < N; i++) {
    float ax, ay, az, gx, gy, gz;
    if (mpuRead(ax, ay, az, gx, gy, gz)) {
      sgx += gx; sgy += gy; sgz += gz;
      sg  += sqrtf(ax * ax + ay * ay + az * az);
      got++;
    }
    digitalWrite(PIN_LED, (i / 10) % 2);   // กะพริบระหว่าง calibrate
    delay(10);
  }
  if (got > N / 2) {
    gyroBiasX = sgx / got; gyroBiasY = sgy / got; gyroBiasZ = sgz / got;
    gravityMag = sg / got;
  }
  Serial.printf("[MPU] bias gx=%.2f gy=%.2f gz=%.2f  |g|=%.3f\n",
                gyroBiasX, gyroBiasY, gyroBiasZ, gravityMag);
}

void sampleImu(uint32_t nowMs) {
  float ax, ay, az, gx, gy, gz;
  if (!mpuRead(ax, ay, az, gx, gy, gz)) {
    if (++imuFailCount >= 20) {                 // อ่านพลาดติดกัน 0.2 s -> init ใหม่
      Serial.println("[MPU] read failed, re-init");
      mpuInit();
      imuFailCount = 0;
    }
    return;
  }
  imuFailCount = 0;

  // ความเร่งที่ไม่ใช่แรงโน้มถ่วง (หน่วย g): |a| - |g|
  float accDyn = fabsf(sqrtf(ax * ax + ay * ay + az * az) - gravityMag);
  // ความเร็วเชิงมุม (°/s) หลังหัก bias
  gx -= gyroBiasX; gy -= gyroBiasY; gz -= gyroBiasZ;
  float gyrMag2 = gx * gx + gy * gy + gz * gz;

  winAccSq += accDyn * accDyn;
  if (accDyn > winAccPeak) winAccPeak = accDyn;
  winGyrSq += gyrMag2;
  winN++;

  if (winN >= IMU_WINDOW) {
    float accRms = sqrtf(winAccSq / winN);
    float gyrRms = sqrtf(winGyrSq / winN);
    if (accRms > MOTION_ACC_RMS) lastMotionMs = nowMs;

#if DEBUG_SERIAL && !PLOT_HR
    static int dbgCount = 0;
    if (++dbgCount >= 10) {                      // ทุก 10 window = 0.5 s
      dbgCount = 0;
      Serial.printf("[IMU] acc_rms=%.3f g  acc_peak=%.3f g  gyro_rms=%.1f dps\n",
                    accRms, winAccPeak, gyrRms);
    }
#endif

    if (mqtt.connected()) {
      char ts[24], msg[200];
      formatTs(ts, sizeof ts);
      snprintf(msg, sizeof msg,
               "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,"
               "\"acc_rms\":%.4f,\"acc_peak\":%.4f,\"gyro_rms\":%.2f}",
               DEVICE_ID, (unsigned long)++imuSeq, ts, accRms, winAccPeak, gyrRms);
      mqtt.publish(TOPIC_IMU, msg);              // QoS 0
    }
    winAccSq = winAccPeak = winGyrSq = 0;
    winN = 0;
  }
}

// =============================================================
//  Heart rate (KY-039 / HW-502): กรองสัญญาณ + หาจังหวะ
// =============================================================
void sampleHr(uint32_t nowMs) {
  hrRaw = analogRead(PIN_HR);
  float x = HR_INVERT ? -(float)hrRaw : (float)hrRaw;

  if (hrBaseline == 0) hrBaseline = x;
  hrBaseline += (x - hrBaseline) * 0.02f;        // DC (แสงรอบข้าง/ความหนานิ้ว) ~0.5 s
  float ac = x - hrBaseline;
  hrSignal += (ac - hrSignal) * 0.25f;           // low-pass ~4-5 Hz ตัด noise

  // envelope ของยอดคลื่น (ลดลงช้า ๆ) ใช้ทำ threshold แบบปรับตัวเอง
  if (hrSignal > hrEnv) hrEnv = hrSignal;
  else                  hrEnv *= 0.995f;
  float threshold = hrEnv * 0.5f;

  bool above = (hrEnv > HR_MIN_AMP) && (hrSignal > threshold);
  if (above && !hrAbove) {                       // ขาขึ้นตัด threshold = 1 จังหวะ
    uint32_t dt = nowMs - lastBeatMs;
    if (dt > 300) {                              // refractory: ไม่เกิน 200 BPM
      if (lastBeatMs != 0 && dt < 1500) {        // ไม่ต่ำกว่า 40 BPM
        ibi[ibiIdx] = dt;
        ibiIdx = (ibiIdx + 1) % 5;
        if (ibiCount < 5) ibiCount++;
        uint32_t sum = 0;
        for (int i = 0; i < ibiCount; i++) sum += ibi[i];
        bpm = 60000.0f * ibiCount / sum;
      }
      lastBeatMs = nowMs;
    }
  }
  hrAbove = above;

  if (nowMs - lastBeatMs > 3000) {               // ไม่เจอจังหวะ 3 วินาที -> reset
    bpm = 0;
    ibiCount = 0;
  }

#if PLOT_HR
  Serial.printf("signal:%.1f threshold:%.1f\n", hrSignal, threshold);
#endif
}

void publishHr(uint32_t nowMs) {
  bool valid  = (bpm >= 40 && bpm <= 200 && ibiCount >= 3);
  bool motion = (lastMotionMs != 0 && nowMs - lastMotionMs < 1000);
#if DEBUG_SERIAL && !PLOT_HR
  char wifiStr[12];
  if (WiFi.status() == WL_CONNECTED) snprintf(wifiStr, sizeof wifiStr, "OK");
  else snprintf(wifiStr, sizeof wifiStr, "--(%d)", (int)WiFi.status());
  char mqttStr[12];
  if (mqtt.connected()) snprintf(mqttStr, sizeof mqttStr, "OK");
  else snprintf(mqttStr, sizeof mqttStr, "--(%d)", mqtt.state());
  Serial.printf("[HR ] raw=%d amp=%.1f bpm=%.0f valid=%d motion=%d | WiFi=%s MQTT=%s\n",
                hrRaw, hrEnv, bpm, valid, motion, wifiStr, mqttStr);
#endif
  if (!mqtt.connected()) return;
  char ts[24], msg[220];
  formatTs(ts, sizeof ts);
  snprintf(msg, sizeof msg,
           "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%s,"
           "\"raw\":%d,\"signal\":%.1f,\"amp\":%.1f,\"bpm\":%.1f,"
           "\"valid\":%s,\"motion\":%s}",
           DEVICE_ID, (unsigned long)++hrSeq, ts,
           hrRaw, hrSignal, hrEnv, valid ? bpm : 0.0f,
           valid ? "true" : "false", motion ? "true" : "false");
  mqtt.publish(TOPIC_HR, msg);                   // QoS 0
}

// =============================================================
//  Wi-Fi / MQTT
// =============================================================
bool mqttConnect() {
  Serial.printf("[MQTT] connecting to %s:%u ... ", MQTT_HOST, MQTT_PORT);
  // LWT: ถ้าหลุดโดยไม่บอก broker จะประกาศ offline แทน (retain)
  bool ok = mqtt.connect(DEVICE_ID, nullptr, nullptr,
                         TOPIC_STATUS, 1, true, MSG_OFFLINE);
  if (ok) {
    mqtt.publish(TOPIC_STATUS, MSG_ONLINE, true);
    Serial.println("OK");
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
    nextSampleUs = micros();                     // connect อาจ block นิดหน่อย -> รีเซ็ตจังหวะ sample
  }
}

void updateLed() {
  if (mqtt.connected()) digitalWrite(PIN_LED, HIGH);            // ติดค้าง = ต่อ broker แล้ว
  else digitalWrite(PIN_LED, (millis() / 250) % 2);             // กะพริบ = ยังไม่ต่อ
}

// =============================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(PIN_LED, OUTPUT);

  snprintf(TOPIC_IMU,    sizeof TOPIC_IMU,    "rlgl/wrist/imu");
  snprintf(TOPIC_HR,     sizeof TOPIC_HR,     "rlgl/wrist/heartrate");
  snprintf(TOPIC_STATUS, sizeof TOPIC_STATUS, "rlgl/status/%s", DEVICE_ID);
  snprintf(MSG_ONLINE,  sizeof MSG_ONLINE,  "{\"device_id\":\"%s\",\"status\":\"online\"}",  DEVICE_ID);
  snprintf(MSG_OFFLINE, sizeof MSG_OFFLINE, "{\"device_id\":\"%s\",\"status\":\"offline\"}", DEVICE_ID);

  // Sensors
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  if (!mpuInit()) Serial.println("[MPU] init FAILED — เช็กสาย SDA/SCL/VCC/GND");
  mpuCalibrate();
  analogReadResolution(12);                      // 0-4095

  // Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                          // ปิด power save -> latency ต่ำลง
  WiFi.setAutoReconnect(true);

  // สแกนดูว่า ESP32 มองเห็น Wi-Fi อะไรบ้าง (ESP32 เห็นเฉพาะ 2.4 GHz)
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
    Serial.println("        1=หา SSID ไม่เจอ, 4=ต่อไม่สำเร็จ (รหัสผิด?), 6=ยังไม่ได้ต่อ/รหัสผิด");
  }

  configTime(0, 0, NTP_SERVER);                  // ts เป็น epoch UTC ms

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(5);                          // หลุดแล้ว broker รู้ภายใน ~7 วินาที
  mqtt.setSocketTimeout(2);
  mqtt.setBufferSize(512);

  nextSampleUs = micros();
  lastHrPubMs  = millis();
}

void loop() {
  // 1) sampling 100 Hz (ทำต่อแม้ Wi-Fi หลุด)
  uint32_t nowUs = micros();
  if ((int32_t)(nowUs - nextSampleUs) >= 0) {
    nextSampleUs += SAMPLE_US;
    if ((int32_t)(nowUs - nextSampleUs) > 50000) nextSampleUs = nowUs + SAMPLE_US; // ตามไม่ทัน -> ข้าม
    uint32_t nowMs = millis();
    sampleImu(nowMs);
    sampleHr(nowMs);
  }

  // 2) heart rate publish 1 Hz
  if (millis() - lastHrPubMs >= HR_PUBLISH_MS) {
    lastHrPubMs += HR_PUBLISH_MS;
    publishHr(millis());
  }

  // 3) network + LED
  handleNetwork();
  updateLed();
}