// ส่วนคำนวณของหน้าเว็บ ไม่แตะ DOM (เทสต์ด้วย node --test tests/frontend/)

export const PLAYING = ["GREEN_LIGHT", "TRANSITION", "RED_LIGHT", "SNIPER"];
const STATE = "rlgl/game/state";
const RESULT = "rlgl/game/result";
const HEART = "rlgl/wrist/heartrate";
const DISTANCE = "rlgl/gateway/distance";
const VISION = "rlgl/gateway/vision";
const IMU = "rlgl/wrist/imu";
export const ROUND_LIMIT_S = 30; // ค่าเริ่มต้นของ game engine
export const BPM_STALE_MS = 10_000;
export const CLOSE_CALL = 0.7; // คะแนนเทียบเกณฑ์ที่ถือว่าเฉียด

const pad = (n) => String(n).padStart(2, "0");
const isNumber = (v) => typeof v === "number" && Number.isFinite(v);

/** 17.94 -> "00:17.9" */
export function clock(seconds) {
  if (!isNumber(seconds)) return "--:--.-";
  const tenths = Math.round(Math.max(0, seconds) * 10);
  const minutes = Math.floor(tenths / 600);
  const rest = tenths - minutes * 600;
  return `${pad(minutes)}:${pad(Math.floor(rest / 10))}.${rest % 10}`;
}

/** 42 -> "00:42" */
export function countdown(seconds) {
  if (!isNumber(seconds)) return "--:--";
  const whole = Math.max(0, Math.round(seconds));
  return `${pad(Math.floor(whole / 60))}:${pad(whole % 60)}`;
}

export function roundLabel(number) {
  return isNumber(number) ? `#${pad(number)}` : "#--";
}

/** เลือกหน้าจอจาก state ล่าสุด */
export function screenFor({ connected, state, roundId, dismissedRound, mode }) {
  if (!connected) return "offline";
  if (state === "CALIBRATE" || state === "CALIBRATE_FAILED" || state === "READY") return "calibrate";
  if (state === "COUNTDOWN") return mode === "sniper" ? "sniper" : "play";
  if (state === "SNIPER") return "sniper";
  if (PLAYING.includes(state)) return "play";
  if (state === "WIN" || state === "GAME_OVER") return dismissedRound === roundId ? "setup" : "result";
  return "setup";
}

/** ช่วงของการนับถอยหลัง จากข้อความ COUNTDOWN ที่ได้รับตอน receivedMs
 * intro = animation (progress 0-1), count = เลข 3-2-1, go = ถึงเวลาเริ่ม */
export function countdownPhase(message, receivedMs, nowMs) {
  const left = (message.starts_in_s ?? 0) - (nowMs - receivedMs) / 1000;
  const count = message.count_s ?? 3;
  const intro = message.intro_s ?? 0;
  if (left > count) return { phase: "intro", progress: intro > 0 ? Math.round((1 - (left - count) / intro) * 100) / 100 : 1 };
  if (left > 0) return { phase: "count", number: Math.ceil(left) };
  return { phase: "go" };
}

/** ความยาวรอบสำหรับแถบเวลา: ค่า remaining_s ที่มากที่สุดในรอบ (ต้นรอบ = เวลาเต็ม) อย่างน้อย ROUND_LIMIT_S */
export function roundLimit(previous, remaining) {
  return Math.max(previous ?? ROUND_LIMIT_S, isNumber(remaining) ? remaining : 0);
}

export function partName(part) {
  return typeof part === "string" ? part.replaceAll("_", " ") : null;
}

/** ข้อความสาเหตุแพ้/ชนะ (ใช้ในตารางประวัติและหน้าผล) */
export function causeText(round) {
  const { result, reason } = round;
  if (result === "ABORTED") {
    return reason === "camera_lost" ? "Round cancelled · camera lost the player"
      : "Round cancelled · a device went offline";
  }
  if (round.mode === "sniper") {
    if (result === "WIN") {
      const hits = round.hits ?? 0;
      return `Pressed the button · ${hits === 0 ? "no hits" : hits === 1 ? "1 hit taken" : `${hits} hits taken`}`;
    }
    if (reason === "hits") return `Hit ${round.hits ?? 3} times by the sniper`;
    if (reason === "timeout") return "Time out before pressing the button";
  }
  if (result === "WIN") return `Touched finish during ${(round.finish_state || "GREEN_LIGHT").replace("_LIGHT", "")}`;
  switch (reason) {
    case "camera": {
      const part = partName(round.caught_part);
      if (!part) return "Moved on RED · seen by the camera";
      const when = isNumber(round.caught_red_s) ? ` · ${round.caught_red_s} s into red` : "";
      return `Moved on RED · ${part} (camera)${when}`;
    }
    case "imu": return "Moved on RED · wrist IMU over threshold";
    case "both": return "Moved on RED · camera and wrist IMU";
    case "timeout": return "Time out before reaching the finish";
    case "finish_during_red": return "Touched the finish sensor during RED LIGHT";
    default: return reason ? `Game over · ${reason}` : "Game over";
  }
}

/** ระยะจาก ultrasonic (cm, วัดจากเส้นชัย) -> ระยะที่เดินมาแล้ว */
export function progress(distanceCm, trackCm) {
  if (!isNumber(distanceCm) || !isNumber(trackCm) || trackCm <= 0) return null;
  const covered = Math.min(trackCm, Math.max(0, trackCm - distanceCm));
  return { covered_m: Math.round(covered / 10) / 10, total_m: Math.round(trackCm / 10) / 10, ratio: covered / trackCm };
}

export function bpmFresh(lastAtMs, nowMs) {
  return isNumber(lastAtMs) && nowMs - lastAtMs <= BPM_STALE_MS;
}

/** บันทึกข้อความระหว่างรอบ เพื่อวาด timeline หลังจบรอบ (เวลาเป็นวินาทีนับจาก state เล่นแรก) */
export class Recorder {
  constructor() {
    this.reset(null);
  }

  reset(roundId) {
    this.roundId = roundId;
    this.mode = "rlgl";
    this.startMs = null;
    this.changes = []; // [{state, ms}]
    this.endMs = null;
    this.samples = { bpm: [], distance: [], motion: [], gyro: [] };
    this.hitsSeen = [];
    this.lastHits = 0;
    this.bpmRest = null;
    this.result = null;
    this.lastState = null;
  }

  message(topic, payload, nowMs) {
    if (topic === STATE) return this.onState(payload, nowMs);
    if (topic === RESULT) {
      if (payload.round_id === this.roundId && !this.result) {
        this.result = payload;
        this.endMs = this.endMs ?? nowMs;
      }
      return;
    }
    const playing = PLAYING.includes(this.lastState) && this.startMs !== null && !this.result;
    if (topic === HEART && isNumber(payload.bpm)) {
      if (this.lastState === "CALIBRATE") this.bpmRest = payload.bpm;
      if (playing) this.samples.bpm.push({ t: this.at(nowMs), v: payload.bpm });
    } else if (topic === IMU && playing && isNumber(payload.gyro_rms)) {
      this.samples.gyro.push({ t: this.at(nowMs), v: payload.gyro_rms });
    } else if (topic === DISTANCE && playing && isNumber(payload.distance_cm)) {
      this.samples.distance.push({ t: this.at(nowMs), cm: payload.distance_cm });
    } else if (topic === VISION && playing && this.lastState === "RED_LIGHT") {
      // การขยับเทียบเกณฑ์: contract = score_ratio, pi_client.py เดิม = score, pi_client.py 28 พ.ย. = motion_score
      const ratio = [payload.score_ratio, payload.score, payload.motion_score].find(isNumber);
      if (isNumber(ratio)) this.samples.motion.push({ t: this.at(nowMs), v: ratio });
    }
  }

  onState(payload, nowMs) {
    if (payload.round_id && payload.round_id !== this.roundId) this.reset(payload.round_id);
    if (payload.mode) this.mode = payload.mode;
    const state = payload.state;
    if (PLAYING.includes(state) && !this.result) {
      if (this.startMs === null) this.startMs = nowMs;
      if (state !== this.lastState) this.changes.push({ state, ms: nowMs });
      if (isNumber(payload.hits) && payload.hits > this.lastHits) {
        this.hitsSeen.push({ t: this.at(nowMs), lives: payload.lives });
      }
    } else if (!PLAYING.includes(state) && PLAYING.includes(this.lastState) && this.endMs === null) {
      this.endMs = nowMs;
    }
    if (isNumber(payload.hits)) this.lastHits = payload.hits;
    this.lastState = state;
  }

  at(ms) {
    return Math.round((ms - this.startMs) / 100) / 10;
  }

  timeline() {
    const segments = this.changes.map((change, i) => {
      const next = this.changes[i + 1];
      const toMs = next ? next.ms : this.endMs;
      return { state: change.state, from: this.at(change.ms), to: toMs === null ? null : this.at(toMs) };
    });
    return {
      round_id: this.roundId,
      mode: this.mode,
      segments,
      end: this.startMs !== null && this.endMs !== null ? this.at(this.endMs) : null,
      bpm: [...this.samples.bpm],
      distance: [...this.samples.distance],
      motion: [...this.samples.motion],
      gyro: [...this.samples.gyro],
      hits: [...this.hitsSeen],
      bpm_rest: this.bpmRest,
      result: this.result,
    };
  }
}

/** ระยะตอนหยุดในช่วงไฟแดง: ค่าแรกที่วัดได้ในช่วงนั้น ถ้าไม่มีใช้ค่าล่าสุดก่อนไฟแดง */
function distanceDuring(timeline, from, to) {
  let before = null;
  for (const sample of timeline.distance) {
    if (sample.t >= from && sample.t <= to) return sample.cm;
    if (sample.t < from) before = sample.cm;
  }
  return before;
}

/** รายการ "what happened" สำหรับหน้าผล */
export function whatHappened(timeline, trackCm) {
  if (!timeline.segments.length) return [];
  const rows = [{ t: 0, kind: "start", title: "Start", detail: timeline.mode === "sniper" ? "Run for the button" : "Walk on GREEN" }];
  const result = timeline.result;
  const caughtOnRed = result && result.result === "GAME_OVER" && ["camera", "imu", "both"].includes(result.reason);
  const reds = timeline.segments.filter((s) => s.state === "RED_LIGHT");
  reds.forEach((red, index) => {
    const isLast = index === reds.length - 1;
    const to = red.to ?? Infinity;
    const scores = timeline.motion.filter((m) => m.t >= red.from && m.t <= to).map((m) => m.v);
    const peak = scores.length ? Math.max(...scores) : null;
    const where = progress(distanceDuring(timeline, red.from, to), trackCm);
    const atText = where ? ` at ${where.covered_m.toFixed(1)} m` : "";
    if (isLast && caughtOnRed) {
      rows.push({ t: red.from, kind: "bad", title: `Red light ${index + 1} - moved`,
        detail: peak !== null ? `Peak motion ${peak.toFixed(2)}${atText}` : `Caught${atText}` });
    } else if (peak !== null && peak >= CLOSE_CALL) {
      rows.push({ t: red.from, kind: "close", title: `Red light ${index + 1} - close call`,
        detail: `Max motion ${peak.toFixed(2)}${atText}` });
    } else {
      rows.push({ t: red.from, kind: "ok", title: `Red light ${index + 1} - survived`,
        detail: where ? `Froze at ${where.covered_m.toFixed(1)} m` : "Froze" });
    }
  });
  timeline.hits.forEach((hit, index) => {
    const lives = hit.lives;
    rows.push({ t: hit.t, kind: "bad", title: `Hit ${index + 1} - ${lives} ${lives === 1 ? "life" : "lives"} left`,
      detail: "Torso stayed in the crosshair" });
  });
  if (result && timeline.end !== null) {
    const sniper = timeline.mode === "sniper";
    if (result.result === "WIN") {
      rows.push({ t: timeline.end, kind: "ok", title: sniper ? "Pressed the button" : "Touched the finish",
        detail: causeText({ ...result, mode: timeline.mode }) });
    } else if (result.result === "GAME_OVER") {
      const title = caughtOnRed ? "Caught" : result.reason === "timeout" ? "Time out" : "Game over";
      rows.push({ t: timeline.end, kind: "bad", title,
        detail: causeText({ ...result, mode: timeline.mode, caught_red_s: undefined }) });
    }
  }
  return rows.sort((a, b) => a.t - b.t);
}
