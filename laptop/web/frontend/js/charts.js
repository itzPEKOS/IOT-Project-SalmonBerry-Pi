// กราฟบน canvas (ไม่ใช้ library ภายนอก)

const C = {
  inset: "#0a0b17", grid: "#24275a", muted: "#8f93c9", text: "#f4f1e8", green: "#3be36b", red: "#ff3b4e",
  yellow: "#ffc93c", cyan: "#47d1ff", pink: "#ff5c8a", violet: "#8b6cff",
};
const LIGHT_COLOR = { GREEN_LIGHT: C.green, TRANSITION: C.yellow, RED_LIGHT: C.red, SNIPER: C.violet };
const PIXEL_FONT = '10px "Press Start 2P", "Lucida Console", monospace';

function prepare(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function steps(ctx, points, x, y, color, width = 3) {
  if (!points.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  points.forEach((p, i) => {
    const px = Math.round(x(p)), py = Math.round(y(p));
    if (i === 0) ctx.moveTo(px, py);
    else { ctx.lineTo(px, Math.round(y(points[i - 1]))); ctx.lineTo(px, py); }
  });
  ctx.stroke();
}

/** ชีพจรย้อนหลัง windowMs (points = [{ms, v}]) */
export function bpmChart(canvas, points, nowMs, windowMs = 60_000) {
  const { ctx, w, h } = prepare(canvas);
  ctx.fillStyle = C.inset;
  ctx.fillRect(0, 0, w, h);
  const recent = points.filter((p) => p.ms >= nowMs - windowMs);
  const values = recent.map((p) => p.v);
  const lo = Math.min(60, ...values) - 5, hi = Math.max(140, ...values) + 5;
  const y = (p) => h - 10 - ((p.v - lo) / (hi - lo)) * (h - 20);
  ctx.strokeStyle = C.grid;
  ctx.lineWidth = 1;
  ctx.font = PIXEL_FONT;
  ctx.fillStyle = C.muted;
  for (const level of [80, 120, 160]) {
    const py = Math.round(y({ v: level })) + 0.5;
    if (py < 4 || py > h - 4) continue;
    ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(w, py); ctx.stroke();
    ctx.fillText(String(level), 4, py - 3);
  }
  if (!recent.length) {
    ctx.fillText("NO HEART RATE DATA", w / 2 - 80, h / 2);
    return;
  }
  steps(ctx, recent, (p) => w - ((nowMs - p.ms) / windowMs) * w, y, C.pink);
}

/** timeline หลังจบรอบ: แถบไฟ, ระยะ, ชีพจร, gyro ข้อมือ (ทั้งรอบ), การขยับจากกล้องตอนไฟแดง (หรือจุดที่โดนยิงในโหมด sniper)
 * gyroLimit = เกณฑ์ gyro ของ game engine (deg/s) */
export function timelineChart(canvas, timeline, trackCm, gyroLimit = 50) {
  const { ctx, w, h } = prepare(canvas);
  const left = 78, right = 12, top = 4, bottom = 22;
  const end = Math.max(timeline.end ?? 0, ...timeline.segments.map((s) => s.to ?? s.from), 1);
  const x = (t) => left + (t / end) * (w - left - right);
  const sniper = timeline.mode === "sniper";
  const rows = sniper
    ? [["LIGHT", 26], ["METERS", 0.3], ["HEART", 0.4], ["WRIST", 0.3]]
    : [["LIGHT", 26], ["METERS", 0.2], ["HEART", 0.3], ["WRIST", 0.25], ["CAMERA", 0.25]];
  const fixed = rows.filter((r) => r[1] > 1).reduce((a, r) => a + r[1], 0);
  const free = h - top - bottom - fixed - 8 * (rows.length - 1);
  let cursor = top;
  const box = {};
  for (const [name, size] of rows) {
    const height = size > 1 ? size : free * size;
    box[name] = { y: cursor, h: height };
    cursor += height + 8;
  }

  ctx.font = PIXEL_FONT;
  ctx.textBaseline = "middle";
  for (const [name] of rows) {
    ctx.fillStyle = C.muted;
    ctx.fillText(name, 0, box[name].y + Math.min(14, box[name].h / 2));
    ctx.fillStyle = C.inset;
    ctx.fillRect(left, box[name].y, w - left - right, box[name].h);
  }

  // แถบไฟ และพื้นหลังจาง ๆ ของแถวอื่น
  for (const seg of timeline.segments) {
    const x0 = x(seg.from), x1 = x(seg.to ?? end);
    ctx.fillStyle = LIGHT_COLOR[seg.state] || C.muted;
    ctx.fillRect(x0, box.LIGHT.y, Math.max(1, x1 - x0), box.LIGHT.h);
    if (seg.state !== "GREEN_LIGHT" && seg.state !== "SNIPER") {
      ctx.globalAlpha = 0.12;
      ctx.fillRect(x0, box.METERS.y, x1 - x0, h - bottom - box.METERS.y);
      ctx.globalAlpha = 1;
    }
  }
  const reds = timeline.segments.filter((s) => s.state === "RED_LIGHT");
  ctx.fillStyle = C.inset;
  reds.forEach((seg, i) => ctx.fillText(`R${i + 1}`, x(seg.from) + 4, box.LIGHT.y + box.LIGHT.h / 2));

  // ระยะที่เดินมาแล้ว (m)
  const m = box.METERS;
  const meters = timeline.distance.map((d) => ({ t: d.t, v: Math.min(trackCm, Math.max(0, trackCm - d.cm)) }));
  steps(ctx, meters, (p) => x(p.t), (p) => m.y + m.h - 4 - (p.v / trackCm) * (m.h - 8), C.cyan);

  // ชีพจร
  const hb = box.HEART;
  const yb = (p) => hb.y + hb.h - 4 - ((p.v - 50) / 140) * (hb.h - 8);
  ctx.fillStyle = C.muted;
  for (const level of [60, 120, 180]) {
    const py = Math.round(yb({ v: level })) + 0.5;
    ctx.fillRect(left, py, w - left - right, 1);
    ctx.fillText(String(level), left + 4, py - 7);
  }
  steps(ctx, timeline.bpm, (p) => x(p.t), yb, C.pink);

  // gyro ข้อมือตลอดรอบ (เส้นประ = เกณฑ์ที่ engine ใช้ตอนไฟแดง)
  const wr = box.WRIST;
  const gyro = timeline.gyro ?? [];
  const gScale = Math.max(gyroLimit * 2, ...gyro.map((p) => p.v));
  const yg = (v) => wr.y + wr.h - 3 - (Math.min(v, gScale) / gScale) * (wr.h - 6);
  steps(ctx, gyro, (p) => x(p.t), (p) => yg(p.v), C.violet, 2);
  ctx.strokeStyle = C.yellow;
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(left, yg(gyroLimit)); ctx.lineTo(w - right, yg(gyroLimit)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = C.muted;
  ctx.fillText(`${gyroLimit}°/s`, w - right - 64, yg(gyroLimit) - 7);
  if (!gyro.length) ctx.fillText("NO WRIST DATA", left + 4, wr.y + wr.h / 2);

  // การขยับจากกล้อง (MediaPipe บน Pi) เทียบเกณฑ์ เฉพาะตอนไฟแดง (1.0 = แพ้)
  if (!sniper) {
    const mo = box.CAMERA;
    const scale = Math.max(1.5, ...timeline.motion.map((p) => p.v));
    const ym = (v) => mo.y + mo.h - (v / scale) * (mo.h - 4);
    for (const p of timeline.motion) {
      ctx.fillStyle = p.v >= 1 ? C.red : p.v >= 0.7 ? C.yellow : C.green;
      ctx.fillRect(x(p.t) - 1.5, ym(p.v), 3, mo.y + mo.h - ym(p.v));
    }
    ctx.strokeStyle = C.yellow;
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(left, ym(1)); ctx.lineTo(w - right, ym(1)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // จุดที่โดนยิง (sniper)
  for (const hit of timeline.hits) {
    ctx.fillStyle = C.red;
    ctx.fillRect(x(hit.t) - 2, box.LIGHT.y, 4, h - bottom - box.LIGHT.y);
  }

  // จุดจบ
  if (timeline.end !== null) {
    const won = timeline.result?.result === "WIN";
    ctx.fillStyle = won ? C.green : C.red;
    ctx.fillRect(x(timeline.end) - 2, box.LIGHT.y, 4, h - bottom - box.LIGHT.y);
  }

  // แกนเวลา
  ctx.fillStyle = C.muted;
  ctx.textBaseline = "alphabetic";
  const stepS = end > 40 ? 10 : 5;
  for (let t = 0; t <= end; t += stepS) ctx.fillText(`${t}s`, Math.min(x(t), w - 30), h - 4);
}
