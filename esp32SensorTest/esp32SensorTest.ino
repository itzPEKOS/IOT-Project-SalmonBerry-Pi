/*
 * ทดสอบเซนเซอร์เส้นชัย: HC-SR04 (วัดระยะ) + KY-036 metal touch (แตะเส้นชัย)
 * ไม่ใช้ Wi-Fi / MQTT  อัปโหลดแล้วเปิด Serial Monitor ที่ 115200
 *
 * ต่อสาย (ขาเดียวกับ esp32Finish.ino)
 *   HC-SR04  VCC -> 3V3 (หรือ 5V + ตัวแบ่งแรงดันที่ ECHO), TRIG -> GPIO5, ECHO -> GPIO6, GND -> GND
 *   KY-036   + -> 3V3, G -> GND, D0 -> GPIO7, A0 ไม่ต้องต่อ  (แตะปลายโลหะ = HIGH กะพริบตามไฟ 50 Hz)
 *            หมุน potentiometer บนโมดูลจนไฟ LED บนบอร์ดติดเฉพาะตอนแตะ
 *   TTP223   VCC -> 3V3, I/O -> GPIO7, GND -> GND           (แตะ = HIGH)
 *   ปุ่มกด    GPIO7 -> ปุ่ม -> GND                          (กด = LOW, ตั้ง TOUCH_ACTIVE_HIGH = false)
 *
 * สิ่งที่จะเห็น
 *   [DIST] 152.3 cm  (min 151.8 max 153.0)   ทุก 0.2 วินาที
 *   [DIST] -- ไม่มีเสียงสะท้อน                เกินระยะ / หันผิดทาง / เซนเซอร์ไม่ทำงาน
 *   [TOUCH] แตะ  (ครั้งที่ 3)
 *   [TOUCH] ปล่อย แตะค้าง 412 ms (สัญญาณกะพริบ 21 ครั้ง ~50 Hz)
 */

#include <Arduino.h>

// ===================== CONFIG =====================
const int PIN_TRIG  = 5;
const int PIN_ECHO  = 6;
const int PIN_TOUCH = 7;
const bool TOUCH_ACTIVE_HIGH = true;     // KY-036 / TTP223 = true, ปุ่มกดลง GND = false
const uint32_t TOUCH_RELEASE_MS = 150;   // ไม่เจอสัญญาณแตะนานเท่านี้ = ปล่อยมือ
// ==================================================

const uint32_t ECHO_TIMEOUT_US = 30000;  // ~5 m
const uint32_t PRINT_MS = 200;

float    minCm = 9999, maxCm = 0;
int      okCount = 0, failCount = 0;
uint32_t lastPrint = 0;

bool     touched = false;
uint32_t touchStart = 0;
int      touchCount = 0;
volatile uint32_t lastActiveMs = 0;      // จดใน interrupt จะได้ไม่พลาดสัญญาณที่กะพริบเร็ว
volatile uint32_t activeEdges = 0;

void IRAM_ATTR onTouchPin() {
  if (digitalRead(PIN_TOUCH) == (TOUCH_ACTIVE_HIGH ? HIGH : LOW)) {
    lastActiveMs = millis();
    activeEdges++;
  }
}

// ยิงคลื่น 1 ครั้ง คืนระยะ (cm) หรือ -1 ถ้าไม่มีเสียงสะท้อน
float readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, ECHO_TIMEOUT_US);
  if (us == 0) return -1;
  return us / 58.0f;                     // เสียง 343 m/s ไป-กลับ
}

void checkTouch() {
  uint32_t now = millis();
  bool active = (digitalRead(PIN_TOUCH) == (TOUCH_ACTIVE_HIGH ? HIGH : LOW)) ||
                (lastActiveMs != 0 && now - lastActiveMs < TOUCH_RELEASE_MS);
  if (active == touched) return;
  touched = active;
  if (touched) {
    touchStart = now;
    touchCount++;
    activeEdges = 0;
    Serial.printf("[TOUCH] แตะ  (ครั้งที่ %d)  t=%lu ms\n", touchCount, (unsigned long)now);
  } else {
    uint32_t held = lastActiveMs - touchStart;
    Serial.printf("[TOUCH] ปล่อย แตะค้าง %lu ms (สัญญาณกะพริบ %lu ครั้ง ~%.0f Hz)\n", (unsigned long)held,
                  (unsigned long)activeEdges, held > 0 ? activeEdges * 1000.0f / held : 0.0f);
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_TRIG, OUTPUT);
  digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_TOUCH, TOUCH_ACTIVE_HIGH ? INPUT_PULLDOWN : INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_TOUCH), onTouchPin, CHANGE);

  Serial.println();
  Serial.println("=== ทดสอบ HC-SR04 + KY-036 (แตะเส้นชัย) ===");
  Serial.printf("TRIG=GPIO%d ECHO=GPIO%d TOUCH=GPIO%d (%s)\n", PIN_TRIG, PIN_ECHO, PIN_TOUCH,
                TOUCH_ACTIVE_HIGH ? "TTP223: แตะ = HIGH" : "ปุ่ม: กด = LOW");
  Serial.printf("ค่าขา TOUCH ตอนเริ่ม = %d (ยังไม่แตะ ควรเป็น %d)\n",
                digitalRead(PIN_TOUCH), TOUCH_ACTIVE_HIGH ? 0 : 1);
}

void loop() {
  float cm = readDistanceCm();
  if (cm >= 2 && cm <= 400) {
    okCount++;
    if (cm < minCm) minCm = cm;
    if (cm > maxCm) maxCm = cm;
  } else {
    failCount++;
  }
  checkTouch();

  uint32_t now = millis();
  if (now - lastPrint >= PRINT_MS) {
    lastPrint = now;
    if (okCount > 0) {
      Serial.printf("[DIST] %6.1f cm  (min %.1f max %.1f, วัดได้ %d/%d ครั้ง)\n",
                    cm > 0 ? cm : maxCm, minCm, maxCm, okCount, okCount + failCount);
    } else {
      Serial.println("[DIST] -- ไม่มีเสียงสะท้อน (เช็กสาย TRIG/ECHO/VCC หรือเอามือบังห่าง 20-50 cm)");
    }
    minCm = 9999; maxCm = 0; okCount = failCount = 0;
  }
  delay(60);                             // HC-SR04 ต้องเว้นอย่างน้อย ~60 ms ต่อการยิง 1 ครั้ง
}
