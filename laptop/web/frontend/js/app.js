// หน้าเว็บเกม: รับข้อมูลสดจาก backend ทาง /ws แล้ววาดหน้าตาม state
// เปิดเป็นจอผู้เล่น: http://<laptop>:8000/          (role=display ส่ง ping ให้ game engine รู้ว่าจอยังอยู่)
// เปิดเป็นจอคนดู:   http://<laptop>:8000/?role=viewer
import {
  PLAYING, ROUND_LIMIT_S, Recorder, bpmFresh, causeText, clock, controlsFor, countdown, countdownPhase, partName,
  progress, roundLabel, roundLimit, screenFor, whatHappened,
} from "./logic.js";
import { fillSprites, sprite } from "./sprites.js";
import { bpmChart, timelineChart } from "./charts.js";

const params = new URLSearchParams(location.search);
const ROLE = params.get("role") === "viewer" ? "viewer" : "display";
const GYRO_LIMIT_DPS = 50; // เกณฑ์ gyro ของ game engine ตอนไฟแดง (ค่าเริ่มต้น --imu-gyro)
const GO_FLASH_MS = 900;   // โชว์ "GO!" ค้างหลังไฟเขียวแรก
// ช่วงที่ถือว่ากำลังเล่น: ล็อกจอผู้เล่นไว้ที่หน้าเกม
const BUSY = ["CALIBRATE", "CALIBRATE_FAILED", "READY", "COUNTDOWN", ...PLAYING];
const AIM_STALE_MS = 1500;
const HIT_FLASH_MS = 1800;
const DEVICES = [["wrist-01", "ESP32 #1"], ["station-01", "ESP32 #2"], ["gateway-01", "PI 5"], ["broker", "BROKER"]];
const T = {
  state: "rlgl/game/state", result: "rlgl/game/result", summary: "rlgl/game/summary", media: "rlgl/game/media",
  mode: "rlgl/game/mode", calibration: "rlgl/game/calibration", heart: "rlgl/wrist/heartrate",
  distance: "rlgl/gateway/distance", vision: "rlgl/gateway/vision", aim: "rlgl/sniper/aim",
  camera: "rlgl/station/camera",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (v) => typeof v === "number" && Number.isFinite(v);

const app = {
  latest: {},
  connected: false,
  config: { stream_url: null, grafana_url: null, track_cm: 300 },
  session: { player: null, next_round_number: null, mode: "rlgl" },
  tab: "live",
  sub: "overview",
  dismissed: null,
  recorder: new Recorder(),
  bpmPoints: [],
  bpmAt: null,
  aimAt: null,
  lastHitAt: 0,
  round: null,          // ข้อมูลรอบปัจจุบันจาก /api/rounds/<id>
  broker: null,
  screen: null,
  recent: [],
  board: { lbMode: "rlgl", result: "all", mode: "", page: 1, history: null, overview: null, detailId: null, detail: null },
};

// ---------------------------------------------------------------- HTTP
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
  }
  return response.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

async function loadSession() {
  try { app.session = await api("/api/session"); } catch { /* backend ยังไม่พร้อม */ }
  schedule();
}

async function loadRecent() {
  try { app.recent = (await api("/api/players?limit=4")).players.map((p) => p.name); } catch { app.recent = []; }
  schedule();
}

const roundFetches = new Map();
async function loadRound(roundId) {
  if (!roundId) return;
  clearTimeout(roundFetches.get(roundId));
  roundFetches.set(roundId, setTimeout(async () => {
    try {
      const data = await api(`/api/rounds/${encodeURIComponent(roundId)}`);
      if (currentRoundId() === roundId) app.round = data;
    } catch { /* รอบยังไม่ถูกบันทึก */ }
    schedule();
  }, 150));
}

async function pollHealth() {
  try { app.broker = (await api("/api/health")).mqtt_connected; } catch { app.broker = null; }
  schedule();
}

// ---------------------------------------------------------------- WebSocket
let socket, heartbeat, retryTimer;
function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws?role=${ROLE}`);
  socket.onopen = () => {
    app.connected = true;
    heartbeat = setInterval(() => socket.readyState === 1 && socket.send("ping"), 2000);
    loadSession();
    schedule();
  };
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const now = Date.now();
    if (data.type === "snapshot") {
      app.latest = { ...data.messages };
      const state = app.latest[T.state];
      if (state) {
        app.recorder.message(T.state, state, now);
        loadRound(state.round_id);
      }
    } else {
      onMessage(data.topic, data.payload, now);
    }
    schedule();
  };
  socket.onclose = () => {
    app.connected = false;
    clearInterval(heartbeat);
    schedule();
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, 1000);
  };
}

function currentRoundId() {
  return app.latest[T.state]?.round_id ?? null;
}

function onMessage(topic, payload, now) {
  const before = app.latest[T.state];
  if (topic === T.state && before?.round_id !== payload.round_id) {
    for (const key of [T.result, T.summary, T.media, T.calibration, T.aim]) delete app.latest[key];
    app.round = null;
    app.bpmPoints = app.bpmPoints.filter((p) => now - p.ms < 60_000);
    loadRound(payload.round_id);
    loadSession();
  }
  if (topic === T.state && num(payload.hits) && num(before?.hits) && payload.hits > before.hits) app.lastHitAt = now;
  if (topic === T.state && payload.state === "CALIBRATE" && before?.state !== "CALIBRATE") loadRound(payload.round_id);
  if (topic === T.state) {
    if (before?.round_id !== payload.round_id) app.limit = null;
    app.stateAt = now;
    if (payload.state !== "IDLE") app.limit = roundLimit(app.limit, payload.remaining_s);
    if (before?.state === "COUNTDOWN" && (payload.state === "GREEN_LIGHT" || payload.state === "SNIPER")) app.goAt = now;
  }
  if (topic === T.heart && num(payload.bpm)) {
    app.bpmAt = now;
    app.bpmPoints.push({ ms: now, v: payload.bpm });
    if (app.bpmPoints.length > 400) app.bpmPoints.splice(0, app.bpmPoints.length - 400);
  }
  if (topic === T.aim) app.aimAt = now;
  if (topic === T.mode && payload.mode) app.session = { ...app.session, mode: payload.mode };
  app.latest[topic] = payload;
  app.recorder.message(topic, payload, now);
  if ([T.result, T.summary, T.media].includes(topic) && payload.round_id === currentRoundId()) {
    loadRound(payload.round_id);
    if (topic === T.result) { loadSession(); if (app.tab === "board") loadBoard(); }
  }
}

// ---------------------------------------------------------------- render loop
let pending = false;
function schedule() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => { pending = false; render(); });
}

function render() {
  const state = app.latest[T.state];
  const playing = state && BUSY.includes(state.state);
  // จอผู้เล่นห้ามค้างอยู่ที่ scoreboard ระหว่างเล่น
  if (ROLE === "display" && playing && app.tab !== "live") setTab("live");
  $("#tab-board").disabled = ROLE === "display" && playing;
  $("#offline").hidden = app.connected;
  renderChips();
  renderSubline();
  if (app.tab === "live") renderLive();
}

function deviceStatus(id) {
  if (id === "broker") return app.broker === null ? "unknown" : app.broker ? "online" : "offline";
  return app.latest[`rlgl/status/${id}`]?.status ?? "unknown";
}

function chipsHtml() {
  return DEVICES.map(([id, label]) => `<span class="chip ${deviceStatus(id)}"><i></i>${label}</span>`).join("");
}

function renderChips() {
  const html = chipsHtml();
  const top = $("#device-chips");
  if (top.innerHTML !== html) top.innerHTML = html;
}

function renderSubline() {
  const state = app.latest[T.state];
  let text;
  if (app.tab === "board") {
    const o = app.board.overview;
    text = o ? `${o.counts.all} rounds played${o.last_ended_at ? ` · last round ${timeOfDay(o.last_ended_at)}` : ""}` : "scoreboard";
  } else if (state && state.state !== "IDLE" && app.screen !== "setup") {
    const mode = (state.mode || app.round?.mode || app.session.mode) === "sniper" ? "sniper mode · " : "";
    const player = app.round?.player ?? "guest";
    text = `${mode}round ${roundLabel(app.round?.number).slice(1)} · player: ${player}`;
  } else {
    const who = app.session.player ? `player: ${app.session.player.name}` : "new player";
    text = `round ${roundLabel(app.session.next_round_number).slice(1)} · ${who}`;
  }
  $("#subline").textContent = text;
}

const timeOfDay = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

// ---------------------------------------------------------------- tabs
function setTab(tab) {
  app.tab = tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#live").hidden = tab !== "live";
  $("#board").hidden = tab !== "board";
  if (tab === "board") { streams(null); loadBoard(); } else { app.screen = null; }
  schedule();
}

// ---------------------------------------------------------------- live screens
function renderLive() {
  const state = app.latest[T.state];
  const screen = screenFor({
    connected: app.connected, state: state?.state, roundId: state?.round_id, dismissedRound: app.dismissed,
    mode: state?.mode,
  });
  const shown = screen === "offline" ? app.screen ?? "setup" : screen;
  if (shown !== app.screen) {
    app.screen = shown;
    for (const name of ["setup", "calibrate", "play", "sniper", "result"]) $(`#v-${name}`).hidden = name !== shown;
    if (shown === "setup") { loadRecent(); loadSession(); }
    streams(shown);
  }
  ({ setup: renderSetup, calibrate: renderCalibrate, play: renderPlay, sniper: renderSniper, result: renderResult })[shown]?.();
  renderControls();
}

// ภาพสดจาก Pi: เปิด connection เฉพาะหน้าที่ต้องใช้ เพื่อไม่ให้ Pi ส่งภาพเปล่า ๆ
function streams(screen) {
  for (const holder of $$("[data-stream]")) {
    const visible = !!screen && holder.closest("section")?.id === `v-${screen}`;
    attachStream(holder, visible);
  }
}

function attachStream(holder, on) {
  let img = $("img", holder);
  let placeholder = $(".placeholder", holder);
  if (!placeholder) {
    placeholder = document.createElement("div");
    placeholder.className = "placeholder";
    holder.prepend(placeholder);
  }
  const url = app.config.stream_url;
  if (!on || !url) {
    if (img) { img.src = ""; img.remove(); }
    placeholder.hidden = false;
    placeholder.innerHTML = url
      ? `${sprite("camera", 6)}<div class="label">CAMERA PAUSED</div>`
      : `${sprite("camera", 6)}<div class="label">NO CAMERA STREAM SET</div><div class="small muted">Set RLGL_STREAM_URL to the Pi's MJPEG address and restart the backend</div>`;
    return;
  }
  if (img) return;
  img = document.createElement("img");
  img.alt = "Live camera from the Pi";
  placeholder.hidden = false;
  placeholder.innerHTML = `${sprite("camera", 6)}<div class="label">CONNECTING TO THE PI CAMERA…</div><div class="small muted">${esc(url)}</div>`;
  img.onload = () => { placeholder.hidden = true; };
  img.onerror = () => {
    placeholder.hidden = false;
    placeholder.innerHTML = `${sprite("camera", 6)}<div class="label red">NO SIGNAL FROM THE PI CAMERA</div><div class="small muted">${esc(url)} · retrying</div>`;
    img.remove();
    setTimeout(() => { if (holder.closest("section") && !holder.closest("section").hidden) attachStream(holder, true); }, 4000);
  };
  img.src = url;
  holder.prepend(img);
}

function stepper(active) {
  const steps = ["ENTER NAME", "CALIBRATE", "PLAY"];
  return steps.map((label, i) => {
    const n = i + 1;
    const cls = n < active ? "done" : n === active ? "now" : "";
    const line = i < steps.length - 1 ? `<div class="step-line ${n < active ? "done" : ""}"></div>` : "";
    return `<div class="step ${cls}"><span>${n < active ? "✓" : n}</span><span>${label}</span></div>${line}`;
  }).join("");
}

function segbar(el, count, on, extra = () => "") {
  if (el.children.length !== count) el.innerHTML = "<i></i>".repeat(count);
  [...el.children].forEach((seg, i) => { seg.className = `${i < on ? "on" : ""} ${extra(i, on)}`.trim(); });
}

// ---- 1) ใส่ชื่อ
const HOW = {
  rlgl: [
    ["heart", "Clip the heart sensor on your finger and wear the wristband"],
    ["runner", "Stand on the START mark and wait for GREEN"],
    ["camera", "Walk on GREEN · freeze on RED (the operator switches the light)"],
    ["flag", "Touch the metal plate at the finish to win"],
  ],
  sniper: [
    ["heart", "Clip the heart sensor on your finger and wear the wristband"],
    ["camera", "The operator aims the camera with the joystick"],
    ["runner", "Keep your body out of the hit box: 1 s inside = HIT (3 lives)"],
    ["flag", `Reach and touch the button within ${ROUND_LIMIT_S} s to win`],
  ],
};

let lastSetupKey = "";
function renderSetup() {
  const mode = app.session.mode || "rlgl";
  const key = JSON.stringify([mode, app.recent, app.session.player, app.latest[T.result], app.connected,
    DEVICES.map(([id]) => deviceStatus(id))]);
  if (key === lastSetupKey) return;
  lastSetupKey = key;
  $("#v-setup [data-stepper]").innerHTML = stepper(1);
  $$("#v-setup [data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#how").innerHTML = HOW[mode].map(([icon, text], i) => `
    <div class="how-row"><div class="how-icon">${sprite(icon, icon === "runner" ? 3 : 4)}</div>
    <div><div class="label">STEP ${i + 1}</div><p>${esc(text)}</p></div></div>`).join("");
  $("#recent").innerHTML = app.recent.map((n) => `<button class="chip-btn" data-name="${esc(n)}">${esc(n)}</button>`).join("");
  $("#recent-label").hidden = app.recent.length === 0;

  const player = app.session.player;
  $("#welcome").hidden = !player;
  if (player) {
    const returning = player.rounds_played > 0;
    $("#welcome-title").textContent = returning ? `WELCOME BACK, ${player.name}!` : `HELLO, ${player.name}!`;
    const modeName = mode === "sniper" ? "sniper" : "red light";
    $("#welcome-text").textContent = returning
      ? [player.best_s !== null ? `Best ${clock(player.best_s)}` : "No win yet", player.rank ? `rank #${player.rank}` : null,
        `${player.rounds_played} ${modeName} round${player.rounds_played === 1 ? "" : "s"} played`].filter(Boolean).join(" · ")
      : `First ${modeName} round · good luck`;
    $("#name-status").textContent = "Ready · press NEXT: CALIBRATE (or the joystick button)";
  } else {
    $("#name-status").textContent = "Press ENTER";
  }

  const result = app.latest[T.result];
  const cancelled = result?.result === "ABORTED" && result.round_id === currentRoundId();
  $("#cancelled").hidden = !cancelled;
  if (cancelled) $("#cancelled-text").textContent = causeText(result);

  $("#device-chips-2").innerHTML = chipsHtml();
  const down = DEVICES.filter(([id]) => deviceStatus(id) !== "online").length;
  const summary = $("#device-summary");
  summary.textContent = down === 0 ? "ALL ONLINE" : `${down} NOT ONLINE`;
  summary.className = `label ${down === 0 ? "green" : "red"}`;
}

async function submitName(raw) {
  const name = (raw ?? $("#name").value).trim().toUpperCase();
  if (!name) { $("#name-status").textContent = "Type a name first"; return; }
  try {
    app.session = await post("/api/session", { name });
    $("#name").value = name;
    lastSetupKey = "";
    loadRecent();
  } catch (error) {
    $("#name-status").textContent = `Name not accepted: ${error.message}`;
  }
  schedule();
}

async function chooseMode(mode) {
  try { app.session = await post("/api/mode", { mode }); } catch (error) { $("#name-status").textContent = error.message; }
  lastSetupKey = "";
  schedule();
}

// ---- ปุ่มคุมเกม: หน้าเว็บ → POST /api/cmd → rlgl/cmd/game → game engine
let opsMessage = "";
async function sendCmd(cmd) {
  try {
    await post("/api/cmd", { cmd });
    opsMessage = "";
  } catch (error) {
    opsMessage = `Not sent: ${error.message}`;
  }
  lastOpsKey = "";
  schedule();
}

function currentControls() {
  const state = app.latest[T.state];
  if (!state || ROLE !== "display") return [];
  const cal = app.latest[T.calibration]?.round_id === state.round_id ? app.latest[T.calibration] : null;
  // ยังไม่รู้ผลเช็ก touch (engine รุ่นเก่า) ถือว่าใช้ได้ ไม่แสดงปุ่ม FINISH
  const touchOk = cal && cal.check_touch ? cal.check_touch === "ok" : true;
  return controlsFor({ state: state.state, mode: state.mode, touchOk });
}

let lastOpsKey = "";
function renderControls() {
  const controls = currentControls();
  const key = JSON.stringify([app.screen, controls, opsMessage]);
  if (key === lastOpsKey) return;
  lastOpsKey = key;
  const hasSpace = controls.some((c) => c.key === "space" && !c.disabled);
  const html = controls.map((c) => `<button class="btn ${c.kind}" data-cmd="${c.cmd}" ${c.disabled ? "disabled" : ""}>${esc(c.label)}</button>`).join("")
    + (controls.length && hasSpace ? '<span class="hint">or press SPACE / the joystick button</span>' : "")
    + (opsMessage ? `<span class="msg">${esc(opsMessage)}</span>` : "");
  for (const box of $$("[data-ops]")) box.innerHTML = box.closest("section")?.id === `v-${app.screen}` ? html : "";
}

// ---- 2) calibrate (เช็ก sensor) → READY หรือ CALIBRATE_FAILED
const SENSOR_ROWS = [
  ["imu", "Wrist motion (MPU6050)", "ESP32 #1 · gyro + accelerometer"],
  ["heart", "Heart rate (KY-039)", "ESP32 #1 · finger sensor"],
  ["camera", "Camera (MediaPipe)", "Pi 5 · sees your whole body"],
  ["distance", "Distance (ultrasonic)", "ESP32 finish · HC-SR04"],
  ["touch", "Finish touch", "ESP32 finish · metal plate"],
  ["station", "Joystick + servo", "ESP32 #2 · camera station"],
];
const CHECK_CLASS = { ok: "ok", wait: "wait", fail: "bad", off: "off" };

function renderCalibrate() {
  const state = app.latest[T.state];
  const ready = state.state === "READY";
  const failed = state.state === "CALIBRATE_FAILED";
  $("#v-calibrate [data-stepper]").innerHTML = stepper(ready ? 3 : 2);
  const roundId = currentRoundId();
  const cal = app.latest[T.calibration]?.round_id === roundId ? app.latest[T.calibration] : null;
  const now = Date.now();

  const badge = $("#cal-badge");
  if (cal?.player_detected) { badge.textContent = "PLAYER DETECTED"; badge.className = "badge"; }
  else if (cal) { badge.textContent = "NO PLAYER IN VIEW"; badge.className = "badge bad"; }
  else { badge.textContent = "WAITING FOR THE PI"; badge.className = "badge warn"; }

  const pct = ready || failed ? 100 : num(cal?.progress) ? Math.round(cal.progress * 100) : null;
  segbar($("#cal-bar"), 25, pct === null ? 0 : Math.round((pct / 100) * 25));
  $("#cal-pct").textContent = pct === null ? "--%" : `${pct}%`;
  $("#cal-bar-label").textContent = ready ? "CALIBRATED" : failed ? "CHECK FAILED" : "CALIBRATING";
  const hold = $("#cal-hold");
  hold.hidden = ready || failed || !(num(cal?.progress) && num(cal?.baseline_s));
  if (!hold.hidden) $("#cal-left").textContent = String(Math.max(0, Math.ceil((1 - cal.progress) * cal.baseline_s)));

  // ผลเช็กของแต่ละ sensor มาจาก game engine (check_<ชื่อ> = ok / wait / fail / off)
  const bpm = bpmFresh(app.bpmAt, now) ? app.latest[T.heart]?.bpm : null;
  const distance = app.latest[T.distance]?.distance_cm;
  const value = {
    imu: { ok: "OK", wait: "...", fail: "NO DATA", off: "NOT USED" },
    heart: { ok: num(bpm) ? `${bpm} BPM` : "OK", wait: "...", fail: "NO PULSE", off: "NOT USED" },
    camera: { ok: "OK", wait: "...", fail: "NOT SEEN", off: "NOT USED" },
    distance: { ok: num(distance) ? `${(distance / 100).toFixed(2)} m` : "OK", wait: "...", fail: "NO DATA", off: "NOT USED" },
    touch: { ok: "ONLINE", wait: "...", fail: "OFFLINE", off: "USE FINISH BUTTON" },
    station: { ok: "ONLINE", wait: "...", fail: "OFFLINE", off: "NOT USED" },
  };
  $("#checks").innerHTML = SENSOR_ROWS.map(([key, title, text]) => {
    const status = cal?.[`check_${key}`] ?? "wait";
    return `<div class="check ${CHECK_CLASS[status] ?? "wait"}"><i></i>
      <div><b>${esc(title)}</b><span class="small">${esc(text)}</span></div><span class="val">${esc(value[key][status] ?? "--")}</span></div>`;
  }).join("");

  const box = $("#cal-status");
  const names = Object.fromEntries(SENSOR_ROWS.map(([key, title]) => [key, title.split(" (")[0]]));
  const skipped = String(cal?.skipped || "").split(",").filter(Boolean).map((k) => names[k] ?? k);
  if (ready) {
    box.className = "cal-status ok";
    box.innerHTML = skipped.length
      ? `<b>READY</b><span>Playing without: ${esc(skipped.join(", "))}</span>`
      : "<b>ALL SENSORS OK</b><span>Ready to start</span>";
  } else if (failed) {
    const list = String(state.failed || cal?.failed || "").split(",").filter(Boolean).map((k) => names[k] ?? k).join(", ");
    box.className = "cal-status bad";
    box.innerHTML = `<b>CHECK FAILED</b><span>${esc(list || "a sensor")} · fix it, then press START to calibrate again</span>`;
  } else {
    box.className = "cal-status";
    box.innerHTML = "<b>CHECKING SENSORS…</b><span>Stand still with your whole body in view</span>";
  }
}

// นับถอยหลังทับหน้าเกม: animation → 3, 2, 1 → GO!
function renderCountdown(section) {
  const state = app.latest[T.state];
  const overlay = $(".cd-overlay", section);
  const now = Date.now();
  let phase = null;
  if (state.state === "COUNTDOWN") phase = countdownPhase(state, app.stateAt ?? now, now);
  else if (app.goAt && now - app.goAt < GO_FLASH_MS) phase = { phase: "go" };
  overlay.hidden = !phase;
  if (!phase) return;
  const key = phase.phase === "count" ? `count-${phase.number}` : phase.phase;
  if (overlay.dataset.key !== key) {
    overlay.dataset.key = key;
    if (phase.phase === "intro") {
      overlay.innerHTML = `<div class="cd-intro"><div class="cd-runner">${sprite("runner", 8)}</div>
        <div class="cd-title">GET READY!</div><div class="cd-sub">${state.mode === "sniper" ? "Dodge the crosshair · reach the button" : "Walk on GREEN · freeze on RED"}</div></div>`;
    } else if (phase.phase === "count") {
      overlay.innerHTML = `<div class="cd-num">${phase.number}</div>`;
    } else {
      overlay.innerHTML = '<div class="cd-num go">GO!</div>';
    }
  }
  schedule(); // วาดต่อจนนับครบ
}

// ---- 3) ไฟเขียว/แดง
const BANNERS = {
  GREEN_LIGHT: ["green", "on-g", "GREEN LIGHT", "Walk to the finish"],
  TRANSITION: ["yellow", "on-y", "SLOW DOWN!", "Red light in a moment · get ready to freeze"],
  RED_LIGHT: ["red", "on-r", "RED LIGHT", "Freeze! The camera is watching"],
  COUNTDOWN: ["grey", "", "GET READY", "The round starts in a moment"],
};

let trackBuilt = false;
function buildTrack() {
  const track = $("#play-track");
  const metres = Math.floor(app.config.track_cm / 100);
  let html = '<div class="ground"></div><div class="ticks">';
  for (let m = 0; m <= metres; m++) html += `<span class="tick" style="left:${4 + (m * 100 / app.config.track_cm) * 88}%">${m}m</span>`;
  html += "</div>";
  for (let i = 0; i < 14; i++) html += `<span class="star" style="left:${(i * 37) % 97}%;top:${12 + ((i * 53) % 55)}%"></span>`;
  html += '<span class="start">START<br>▮<br>▮<br>▮</span>';
  html += `<span class="finish">· FINISH<br>${sprite("flag", 5)}</span>`;
  html += `<span class="runner" id="runner" style="left:4%"><span class="tag" id="frozen" hidden>FROZEN?</span><span id="runner-art"></span></span>`;
  track.innerHTML = html;
  trackBuilt = true;
}

let runnerFrame = 0;
function renderPlay() {
  const state = app.latest[T.state];
  const [color, lightClass, title, sub] = BANNERS[state.state] ?? BANNERS.GREEN_LIGHT;
  $("#play-banner").className = `banner ${color}`;
  $("#play-light").className = `light ${lightClass}`;
  $("#play-title").textContent = title;
  $("#play-sub").textContent = sub;
  $("#play-time").textContent = countdown(state.remaining_s);
  const limit = app.limit ?? ROUND_LIMIT_S;
  segbar($("#play-timebar"), 20, num(state.remaining_s) ? Math.ceil((state.remaining_s / limit) * 20) : 0);
  $("#play-limit").textContent = `Round limit ${Math.round(limit)} s`;

  if (!trackBuilt) buildTrack();
  const where = progress(app.latest[T.distance]?.distance_cm, app.config.track_cm);
  $("#play-dist").textContent = where ? `${where.covered_m.toFixed(1)} m / ${where.total_m.toFixed(1)} m` : `-- m / ${(app.config.track_cm / 100).toFixed(1)} m`;
  $("#play-pct").textContent = where ? `${Math.round(where.ratio * 100)}%` : "--%";
  const segs = where ? Math.round(where.ratio * 25) : 0;
  segbar($("#play-distbar"), 25, segs, (i, on) => (i === on - 1 ? "head" : ""));
  $("#runner").style.left = `${4 + (where ? where.ratio : 0) * 88}%`;
  const walking = state.state === "GREEN_LIGHT";
  const art = walking && runnerFrame % 2 ? "runner2" : "runner";
  const runnerArt = $("#runner-art");
  if (runnerArt.dataset.art !== art) { runnerArt.dataset.art = art; runnerArt.innerHTML = sprite(art, 6); }
  $("#frozen").hidden = state.state !== "RED_LIGHT";
  renderHearts($("#v-play"));
  renderCountdown($("#v-play"));
}

function renderHearts(section) {
  const timeline = app.recorder.timeline();
  const values = timeline.round_id === currentRoundId() ? timeline.bpm.map((b) => b.v) : [];
  const fresh = bpmFresh(app.bpmAt, Date.now());
  const bpm = fresh ? app.latest[T.heart]?.bpm : null;
  const rest = timeline.bpm_rest ?? app.round?.bpm_rest;
  const facts = [
    values.length ? `AVG ${Math.round(values.reduce((a, b) => a + b, 0) / values.length)}` : null,
    values.length ? `MAX ${Math.max(...values)}` : null,
    num(rest) ? `REST ${Math.round(rest)}` : null,
  ].filter(Boolean).join(" · ");
  for (const panel of $$("[data-heart]", section)) {
    if (!panel.dataset.built) {
      const compact = panel.hasAttribute("data-compact");
      panel.innerHTML = `<div style="text-align:center">${sprite("heart", compact ? 6 : 8)}<br><span class="live-tag" data-live>■ LIVE</span></div>
        <div><div class="label">HEART RATE</div><div class="bpm"><span data-bpm>--</span><small>BPM</small></div><div class="label" data-facts style="color:var(--text)"></div></div>
        ${compact ? "" : '<canvas class="chart" data-chart></canvas>'}`;
      if (compact) panel.style.gridTemplateColumns = "auto 1fr";
      panel.dataset.built = "1";
    }
    $("[data-bpm]", panel).textContent = bpm ?? "--";
    $("[data-facts]", panel).textContent = facts || (fresh ? "" : "no reading for 10 s");
    const tag = $("[data-live]", panel);
    tag.textContent = fresh ? "■ LIVE" : "NO SIGNAL";
    tag.className = `live-tag ${fresh ? "" : "stale"}`;
    const chart = $("[data-chart]", panel);
    if (chart) bpmChart(chart, app.bpmPoints, Date.now());
  }
}

// ---- 4) sniper
function renderSniper() {
  const state = app.latest[T.state];
  const now = Date.now();
  const aim = app.latest[T.aim]?.round_id === state.round_id && now - (app.aimAt ?? 0) < AIM_STALE_MS ? app.latest[T.aim] : null;
  const justHit = now - app.lastHitAt < HIT_FLASH_MS;
  const lock = aim && num(aim.lock_s) ? aim.lock_s : 0;
  const limit = aim && num(aim.lock_limit_s) ? aim.lock_limit_s : 1;

  let tag, alert, title, text;
  if (justHit) { tag = ["hit", "HIT! −1 LIFE"]; alert = "red"; title = "HIT!"; text = "You stayed in the crosshair too long"; }
  else if (!aim) { tag = ["safe", "NO AIM DATA"]; alert = "grey"; title = "WAITING…"; text = "No aim data from the Pi yet"; }
  else if (aim.in_box) { tag = ["", `! LOCKING ON · ${lock.toFixed(1)} s`]; alert = "yellow"; title = "LOCKING ON!"; text = "MOVE! Get out of the crosshair"; }
  else { tag = ["safe", "SAFE"]; alert = "green"; title = "SAFE"; text = "Keep moving to the button"; }
  $("#sn-tag").className = `lock-tag ${tag[0]}`;
  $("#sn-tag").textContent = tag[1];
  $("#sn-alert").className = `alert ${alert}`;
  $("#sn-alert-title").textContent = title;
  $("#sn-alert-text").textContent = text;
  $("#sn-box").classList.toggle("locked", !!aim?.in_box);
  $("#sn-offset").textContent = aim && num(aim.offset_px) ? `${(aim.part || "torso").toUpperCase()} ${Math.round(aim.offset_px)} px FROM CENTER` : "";
  segbar($("#sn-lockbar"), 20, Math.round(Math.min(1, lock / limit) * 20), (i) => (i >= 12 ? "hot" : ""));
  $("#sn-lock").textContent = `${lock.toFixed(1)} / ${limit.toFixed(1)} s`;

  const cam = app.latest[T.camera];
  const signed = (v) => (num(v) ? `${v - 90 >= 0 ? "+" : ""}${Math.round(v - 90)}°` : "--°");
  $("#sn-pan").textContent = `PAN ${signed(cam?.pan)}`;
  $("#sn-tilt").textContent = `TILT ${signed(cam?.tilt)}`;

  const lives = num(state.lives) ? state.lives : 3;
  const total = Math.max(3, lives + (state.hits ?? 0));
  const livesKey = `${lives}/${total}`;
  const livesEl = $("#sn-lives");
  if (livesEl.dataset.key !== livesKey) {
    livesEl.dataset.key = livesKey;
    livesEl.innerHTML = Array.from({ length: total }, (_, i) => `<span class="${i < lives ? "" : "lost"}">${sprite("runner", 5)}</span>`).join("");
  }
  const hits = state.hits ?? 0;
  $("#sn-hits").textContent = `${hits} HIT${hits === 1 ? "" : "S"} TAKEN`;
  $("#sn-time").textContent = countdown(state.remaining_s);
  $("#sn-dodges").textContent = num(state.dodges) ? String(state.dodges) : "-";
  const where = progress(app.latest[T.distance]?.distance_cm, app.config.track_cm);
  $("#sn-dist").textContent = where ? `${where.covered_m.toFixed(1)} m / ${where.total_m.toFixed(1)} m` : "-- m";
  segbar($("#sn-distbar"), 25, where ? Math.round(where.ratio * 25) : 0, (i, on) => (i === on - 1 ? "head" : ""));
  $("#sn-limit").textContent = `limit ${Math.round(app.limit ?? ROUND_LIMIT_S)} s`;
  renderHearts($("#v-sniper"));
  renderCountdown($("#v-sniper"));
}

// ---- 5) ผลรอบ
let lastTimelineKey = "";
function renderResult() {
  const roundId = currentRoundId();
  const result = app.latest[T.result]?.round_id === roundId ? app.latest[T.result] : null;
  const round = app.round?.round_id === roundId ? app.round : null;
  const summary = app.latest[T.summary]?.round_id === roundId ? app.latest[T.summary] : {};
  const data = { ...(result || {}), ...summary, ...(round || {}) };
  const mode = data.mode || app.latest[T.state]?.mode || "rlgl";
  const won = (data.result || app.latest[T.state]?.state) === "WIN";

  $("#res-banner").className = `result-banner ${won ? "win" : "lose"}`;
  $("#res-sprite").innerHTML = sprite(won ? "trophy" : "skull", 8);
  $("#res-title").textContent = won ? "YOU WIN!" : "GAME OVER";
  $("#res-sub").textContent = data.result ? causeText({ ...data, mode }) : "Waiting for the result…";
  $("#res-time-label").textContent = won ? "FINISH TIME" : "TIME";
  $("#res-time").textContent = clock(data.duration_s);

  const standing = round?.standing;
  const ribbon = $("#res-ribbon");
  ribbon.hidden = !standing;
  if (standing) {
    ribbon.textContent = standing.new_record ? `★ NEW RECORD · #1 ON THE LEADERBOARD`
      : standing.personal_best ? `★ PERSONAL BEST · RANK #${standing.rank}` : `RANK #${standing.rank}`;
  }
  confetti(won);

  const heartCard = ["HEART RATE", num(data.bpm_avg) || num(data.bpm_max) ? `${num(data.bpm_avg) ? Math.round(data.bpm_avg) : "--"}/${num(data.bpm_max) ? Math.round(data.bpm_max) : "--"}` : "--", "avg / max BPM"];
  let cards;
  if (mode === "sniper") {
    cards = [
      [won ? "FINISH TIME" : "TIME", clock(data.duration_s), won ? beatenText(standing) : causeText({ ...data, mode })],
      ["HITS TAKEN", num(data.hits) ? `${data.hits}/3` : "--", "1 s inside the hit box = 1 hit"],
      ["DODGES", num(data.dodges) ? String(data.dodges) : "--", "quick moves from the wrist IMU"],
      heartCard,
    ];
  } else if (won) {
    cards = [
      ["FINISH TIME", clock(data.duration_s), beatenText(standing)],
      ["RED LIGHTS SURVIVED", num(data.red_light_count) ? String(data.red_light_count) : "--", "never crossed the line"],
      ["CLOSEST CALL", num(data.closest_call) ? data.closest_call.toFixed(2) : "--", "highest motion score on RED (limit 1.0)"],
      heartCard,
    ];
  } else {
    const by = { camera: "CAMERA", imu: "WRIST", both: "BOTH", timeout: "TIME", finish_during_red: "FINISH" }[data.reason] ?? "--";
    cards = [
      ["CAUGHT BY", by, causeText({ ...data, mode })],
      ["BODY PART", partName(data.caught_part)?.toUpperCase() ?? "--", num(data.caught_red_s) ? `${data.caught_red_s} s into RED LIGHT` : "where the move was seen"],
      ["RED LIGHTS", num(data.red_light_count) ? String(data.red_light_count) : "--", "red lights in this round"],
      heartCard,
    ];
  }
  $("#res-cards").innerHTML = cards.map(([label, value, text]) => `<div class="panel card"><div class="label">${esc(label)}</div>
    <div class="v">${esc(value)}</div><p>${esc(text)}</p></div>`).join("");

  // timeline จากข้อมูลที่หน้านี้เห็นระหว่างรอบ
  const timeline = app.recorder.timeline();
  const has = timeline.round_id === roundId && timeline.segments.length > 0;
  $("#tl-canvas").hidden = !has;
  $("#tl-legend").hidden = !has;
  $("#tl-empty").hidden = has;
  $("#tl-title").textContent = has && timeline.end !== null ? `ROUND TIMELINE · ${timeline.end.toFixed(1)} s` : "ROUND TIMELINE";
  const key = JSON.stringify([roundId, timeline.end, timeline.segments.length, timeline.bpm.length, timeline.gyro.length, $("#tl-canvas").clientWidth, !!timeline.result]);
  if (has && key !== lastTimelineKey) {
    lastTimelineKey = key;
    timelineChart($("#tl-canvas"), timeline, app.config.track_cm, GYRO_LIMIT_DPS);
    $("#tl-legend").innerHTML = [
      ["#47d1ff", "distance (ultrasonic)"], ["#ff5c8a", "heart rate"], ["#8b6cff", "wrist gyro (whole round)"],
      ...(mode === "sniper" ? [["#ff3b4e", "hit"]]
        : [["#3be36b", "camera: still (RED only)"], ["#ffc93c", "close call"], ["#ff3b4e", "moved (≥ 1.0)"]]),
    ].map(([c, t]) => `<span><i style="background:${c}"></i>${t}</span>`).join("");
  }
  const rows = has ? whatHappened(timeline, app.config.track_cm) : [];
  $("#res-happened").innerHTML = rows.length ? rows.map((r) => `<div class="row"><span class="t">${r.t.toFixed(1)}s</span><i class="k-${r.kind}"></i>
    <div><b>${esc(r.title)}</b><span>${esc(r.detail)}</span></div></div>`).join("")
    : '<div class="empty">Not recorded on this screen</div>';

  const media = app.latest[T.media]?.round_id === roundId ? app.latest[T.media] : null;
  const video = round?.video_url ?? media?.video_url ?? null;
  $("#res-replay").disabled = !video;
  $("#tl-replay").disabled = !video;
  $("#res-replay").dataset.url = video ?? "";
  $("#res-video-status").textContent = video ? "Replay ready" : "Waiting for the replay from the Pi…";
}

function beatenText(standing) {
  if (!standing) return "time from GREEN to the finish";
  if (standing.beaten) return `${standing.beaten.by_s} s faster than ${standing.beaten.player}`;
  if (standing.new_record) return "first record on the board";
  return standing.personal_best ? "your personal best" : `rank #${standing.rank} on the board`;
}

let confettiFor = null;
function confetti(on) {
  const banner = $("#res-banner");
  const id = on ? currentRoundId() : null;
  if (confettiFor === id) return;
  confettiFor = id;
  $$(".confetti", banner).forEach((c) => c.remove());
  if (!on) return;
  const colors = ["#ff3b4e", "#ffc93c", "#47d1ff", "#f4f1e8", "#ff5c8a"];
  for (let i = 0; i < 18; i++) {
    const c = document.createElement("i");
    c.className = "confetti";
    c.style.left = `${35 + ((i * 37) % 60)}%`;
    c.style.top = `${8 + ((i * 29) % 80)}%`;
    c.style.background = colors[i % colors.length];
    c.style.transform = `rotate(${(i * 23) % 90}deg)`;
    banner.append(c);
  }
}

function openVideo(url) {
  if (!url) return;
  const video = $("#modal-video");
  video.src = url;
  video.playbackRate = 1;
  $("#modal-slow").textContent = "SLOW-MO 0.25×";
  $("#video-modal").hidden = false;
  video.play().catch(() => {});
}

function closeVideo() {
  const video = $("#modal-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#video-modal").hidden = true;
}

// ---------------------------------------------------------------- scoreboard
async function loadBoard() {
  const b = app.board;
  try {
    const [lb, all] = await Promise.all([
      api(`/api/leaderboard?limit=7&mode=${b.lbMode}`),
      api("/api/rounds?page=1&page_size=1"),
    ]);
    b.overview = { entries: lb.entries, counts: all.counts, last_ended_at: all.last_ended_at, last: all.rounds[0] ?? null };
    const q = new URLSearchParams({ result: b.result, page: b.page, page_size: 7 });
    if (b.mode) q.set("mode", b.mode);
    b.history = await api(`/api/rounds?${q}`);
    if (b.sub === "detail" && b.detailId) b.detail = await api(`/api/rounds/${encodeURIComponent(b.detailId)}`);
  } catch (error) {
    b.error = error.message;
  }
  renderBoard();
  schedule();
}

function setSub(sub) {
  app.board.sub = sub;
  $$(".subtab").forEach((t) => t.classList.toggle("active", t.dataset.sub === sub));
  for (const name of ["overview", "history", "detail"]) $(`#b-${name}`).hidden = name !== sub;
  if (sub !== "detail") $("#d-video").pause();
  renderBoard();
}

async function openDetail(roundId) {
  app.board.detailId = roundId;
  app.board.detail = null;
  setSub("detail");
  try { app.board.detail = await api(`/api/rounds/${encodeURIComponent(roundId)}`); } catch { /* ไม่พบรอบ */ }
  renderBoard();
}

function renderBoard() {
  const b = app.board;
  const o = b.overview;
  const last = o?.last;
  const sub = $("#sub-detail");
  sub.textContent = last ? `LAST ROUND ${roundLabel(last.number)}` : "LAST ROUND";
  sub.disabled = !last && !b.detailId;

  // overview
  $$("[data-lb]").forEach((x) => x.classList.toggle("active", x.dataset.lb === b.lbMode));
  $("#lb-hint").textContent = b.lbMode === "sniper" ? "Wins only · time to reach the button" : "Wins only · time from GREEN to touching the finish sensor";
  $("#lb").innerHTML = o?.entries?.length ? o.entries.map((e) => `<div class="board-row"><span class="rank">#${e.rank}</span>
      <span class="name">${esc(e.player)}</span><span class="time">${clock(e.duration_s)}</span>
      <span class="bpmx">${num(e.bpm_max) ? `MAX ${Math.round(e.bpm_max)} BPM` : ""}</span></div>`).join("")
    : '<div class="empty">No wins yet in this mode. Play a round and put your name here.</div>';
  if (last) {
    $("#last-label").textContent = `LAST ROUND · ${roundLabel(last.number)}`;
    $("#last-title").textContent = `${last.player ?? "GUEST"} - ${last.result === "WIN" ? "WIN" : "GAME OVER"}`;
    $("#last-text").textContent = causeText(last);
    $("#last-card").style.borderColor = last.result === "WIN" ? "var(--green)" : "var(--red)";
  }
  $("#last-open").disabled = !last;
  $("#hist-count").textContent = o ? `${o.counts.all} round${o.counts.all === 1 ? "" : "s"} played` : "--";

  // history
  const h = b.history;
  $$("[data-result]").forEach((x) => {
    x.classList.toggle("active", x.dataset.result === b.result);
    const n = h?.counts ? { all: h.counts.all, win: h.counts.wins, loss: h.counts.losses }[x.dataset.result] : null;
    x.textContent = `${{ all: "ALL", win: "WINS", loss: "LOSSES" }[x.dataset.result]}${n === null ? "" : ` ${n}`}`;
  });
  $$("[data-hmode]").forEach((x) => x.classList.toggle("active", x.dataset.hmode === b.mode));
  $("#h-rows").innerHTML = h?.rounds?.length ? h.rounds.map((r) => {
    const win = r.result === "WIN";
    return `<tr><td class="num">${roundLabel(r.number)}${r.mode === "sniper" ? '<br><span class="mode-tag">SNIPER</span>' : ""}</td>
      <td>${esc(r.player ?? "GUEST")}</td>
      <td><span class="badge-res ${win ? "win" : "lose"}">${win ? "WIN" : "LOSE"}</span></td>
      <td class="t">${clock(r.duration_s)}</td><td class="b">${num(r.bpm_max) ? Math.round(r.bpm_max) : "--"}</td>
      <td class="cause ${win ? "" : "lose"}">${esc(causeText(r))}</td>
      <td><button class="link" data-open="${esc(r.round_id)}">▶ REPLAY</button></td></tr>`;
  }).join("") : '<tr><td colspan="7"><div class="empty">No rounds here yet</div></td></tr>';
  if (h) {
    const pages = Math.max(1, Math.ceil(h.total / h.page_size));
    $("#h-showing").textContent = `Showing ${h.rounds.length} of ${h.total} rounds`;
    const start = Math.max(1, Math.min(h.page - 2, pages - 4));
    const numbers = [];
    for (let p = start; p <= Math.min(pages, start + 4); p++) numbers.push(`<button data-page="${p}" class="${p === h.page ? "current" : ""}">${p}</button>`);
    $("#h-pages").innerHTML = `<button data-page="${h.page - 1}" ${h.page <= 1 ? "disabled" : ""}>◀ PREV</button>${numbers.join("")}
      <button data-page="${h.page + 1}" ${h.page >= pages ? "disabled" : ""}>NEXT ▶</button>`;
  }

  if (b.sub === "detail") renderDetail();
}

function renderDetail() {
  const d = app.board.detail;
  if (!d) { $("#d-title").textContent = "ROUND"; $("#d-sub").textContent = "loading…"; return; }
  const isLast = app.board.overview?.last?.round_id === d.round_id;
  const won = d.result === "WIN";
  $("#d-title").textContent = `${isLast ? "LAST ROUND" : "ROUND"} · ${roundLabel(d.number)}`;
  $("#d-sub").textContent = [d.player ?? "GUEST", d.ended_at ? new Date(d.ended_at).toLocaleString() : null, d.mode === "sniper" ? "sniper mode" : "red light mode"].filter(Boolean).join(" · ");
  const video = $("#d-video");
  if (d.video_url) {
    if (video.dataset.src !== d.video_url) { video.src = d.video_url; video.dataset.src = d.video_url; video.playbackRate = 1; }
    video.hidden = false; $("#d-novideo").hidden = true;
  } else {
    video.pause(); video.removeAttribute("src"); video.dataset.src = ""; video.hidden = true; $("#d-novideo").hidden = false;
  }
  for (const id of ["#d-play", "#d-jump", "#d-slow"]) $(id).disabled = !d.video_url;

  $("#d-result").textContent = won ? "YOU WIN!" : "GAME OVER";
  $("#d-result").style.color = won ? "var(--green)" : "var(--red)";
  const who = d.player ?? "The player";
  $("#d-sentence").textContent = won ? `${who} finished in ${clock(d.duration_s)}.`
    : d.reason === "camera" && num(d.caught_red_s) ? `${who} was caught by the camera ${d.caught_red_s} s after the light turned red.`
      : `${who}: ${causeText(d)}.`;
  const rows = [];
  if (d.mode === "sniper") {
    rows.push(["Result", causeText(d)], ["Hits taken", num(d.hits) ? `${d.hits} of 3 lives` : "--"], ["Dodges", num(d.dodges) ? String(d.dodges) : "--"]);
  } else if (won) {
    rows.push(["Finished during", (d.finish_state || "--").replace("_", " ")], ["Red lights survived", num(d.red_light_count) ? String(d.red_light_count) : "--"],
      ["Closest call", num(d.closest_call) ? `${d.closest_call.toFixed(2)} × threshold` : "--"]);
  } else {
    const camera = ["camera", "both"].includes(d.reason), wrist = ["imu", "both"].includes(d.reason);
    rows.push(["Cause", causeText(d).split(" · ")[0]]);
    if (camera || wrist) rows.push(["Detected by", `Camera ${camera ? "✓" : "✗"} · Wrist ${wrist ? "✓" : "✗"}`]);
    if (d.caught_part) rows.push(["Body part", partName(d.caught_part)]);
    if (num(d.caught_red_s)) rows.push(["When", `${d.caught_red_s} s into RED LIGHT`]);
    if (num(d.caught_score_ratio)) rows.push(["Peak score", `${d.caught_score_ratio.toFixed(2)} × threshold`]);
  }
  $("#d-cause").style.borderColor = won ? "var(--green)" : "var(--red)";
  $("#d-cause").innerHTML = `<div class="label ${won ? "green" : "red"}">${won ? "RESULT" : "CAUSE"}</div>` +
    rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  const where = progress(d.distance_cm, app.config.track_cm);
  $("#d-mini").innerHTML = [
    ["TIME", clock(d.duration_s), ""], ["DISTANCE", where ? `${where.covered_m.toFixed(1)} m` : "--", ""],
    ["AVG BPM", num(d.bpm_avg) ? String(Math.round(d.bpm_avg)) : "--", "pink"], ["MAX BPM", num(d.bpm_max) ? String(Math.round(d.bpm_max)) : "--", "pink"],
  ].map(([k, v, c]) => `<div><div class="label">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join("");
  const grafana = app.config.grafana_url || `http://${location.hostname}:3000/d/rlgl-main`;
  const range = d.started_at && d.ended_at ? `${grafana.includes("?") ? "&" : "?"}from=${d.started_at - 5000}&to=${d.ended_at + 5000}` : "";
  $("#d-grafana").href = grafana + range;
}

// ---------------------------------------------------------------- events
function bindEvents() {
  $$(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $$(".subtab").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.sub === "detail") { const last = app.board.overview?.last; if (last && !app.board.detailId) return openDetail(last.round_id); }
    setSub(b.dataset.sub);
  }));

  const input = $("#name");
  input.addEventListener("input", () => {
    const clean = input.value.toUpperCase().replace(/[^A-Z0-9_-]/g, "").slice(0, 10);
    if (clean !== input.value) input.value = clean;
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submitName(); });
  $("#name-go").addEventListener("click", async () => {
    // มีชื่อในช่อง = บันทึกชื่อก่อน แล้วสั่ง calibrate (ช่องว่าง + มีผู้เล่นอยู่แล้ว = calibrate เลย)
    if ($("#name").value.trim() || !app.session.player) {
      await submitName();
      if (!app.session.player) return;
    }
    sendCmd("start");
  });
  $("#recent").addEventListener("click", (e) => { const n = e.target.closest("[data-name]")?.dataset.name; if (n) submitName(n); });
  $("#guest").addEventListener("click", async () => {
    try { app.session = await api("/api/session", { method: "DELETE" }); } catch { /* ignore */ }
    input.value = ""; lastSetupKey = ""; schedule();
    sendCmd("start");
  });
  $$("#v-setup [data-mode]").forEach((b) => b.addEventListener("click", () => chooseMode(b.dataset.mode)));

  $("#res-again").addEventListener("click", () => { app.dismissed = currentRoundId(); lastSetupKey = ""; schedule(); });
  $("#res-board").addEventListener("click", () => { setTab("board"); setSub("overview"); });
  $("#res-replay").addEventListener("click", () => openVideo($("#res-replay").dataset.url));
  $("#tl-replay").addEventListener("click", () => openVideo($("#res-replay").dataset.url));
  $("#modal-close").addEventListener("click", closeVideo);
  $("#video-modal").addEventListener("click", (e) => { if (e.target.id === "video-modal") closeVideo(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#video-modal").hidden) closeVideo(); });
  // ปุ่ม SPACE = ปุ่มหลัก (START / สลับไฟ) ยกเว้นตอนพิมพ์ในช่องข้อความ
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || e.target.closest("input, textarea, button")) return;
    const main = currentControls().find((c) => c.key === "space" && !c.disabled);
    if (!main || app.tab !== "live") return;
    e.preventDefault();
    sendCmd(main.cmd);
  });
  document.addEventListener("click", (e) => {
    const cmd = e.target.closest("[data-ops] [data-cmd]")?.dataset.cmd;
    if (cmd) sendCmd(cmd);
  });
  $("#modal-slow").addEventListener("click", () => toggleSlow($("#modal-video"), $("#modal-slow")));

  $$("[data-lb]").forEach((b) => b.addEventListener("click", () => { app.board.lbMode = b.dataset.lb; loadBoard(); }));
  $("#last-open").addEventListener("click", () => { const last = app.board.overview?.last; if (last) openDetail(last.round_id); });
  $("#hist-open").addEventListener("click", () => setSub("history"));
  $$("[data-result]").forEach((b) => b.addEventListener("click", () => { app.board.result = b.dataset.result; app.board.page = 1; loadBoard(); }));
  $$("[data-hmode]").forEach((b) => b.addEventListener("click", () => { app.board.mode = b.dataset.hmode; app.board.page = 1; loadBoard(); }));
  $("#h-pages").addEventListener("click", (e) => { const p = e.target.closest("[data-page]"); if (p && !p.disabled) { app.board.page = Number(p.dataset.page); loadBoard(); } });
  $("#h-rows").addEventListener("click", (e) => { const id = e.target.closest("[data-open]")?.dataset.open; if (id) openDetail(id); });
  $("#d-back").addEventListener("click", () => setSub("history"));
  $("#d-again").addEventListener("click", () => { app.dismissed = currentRoundId(); setTab("live"); });

  const video = $("#d-video");
  $("#d-play").addEventListener("click", () => (video.paused ? video.play() : video.pause()));
  video.addEventListener("play", () => { $("#d-play").textContent = "❚❚ PAUSE"; });
  video.addEventListener("pause", () => { $("#d-play").textContent = "▶ PLAY"; });
  // คลิป = ทั้งรอบ + 2 วินาทีหลังจบ: กระโดดไป 1 วินาทีก่อนจุดตัดสิน
  $("#d-jump").addEventListener("click", () => {
    if (!num(video.duration)) return;
    video.currentTime = Math.max(0, video.duration - 3);
    video.play().catch(() => {});
  });
  $("#d-slow").addEventListener("click", () => toggleSlow(video, $("#d-slow")));
  window.addEventListener("resize", () => { lastTimelineKey = ""; schedule(); });
}

function toggleSlow(video, button) {
  const slow = video.playbackRate !== 0.25;
  video.playbackRate = slow ? 0.25 : 1;
  button.textContent = slow ? "NORMAL SPEED 1×" : "SLOW-MO 0.25×";
}

// ---------------------------------------------------------------- start
async function start() {
  fillSprites();
  bindEvents();
  try { app.config = { ...app.config, ...(await api("/api/config")) }; } catch { /* ใช้ค่าตั้งต้น */ }
  connect();
  pollHealth();
  setInterval(pollHealth, 5000);
  // วาดซ้ำเป็นระยะ: ค่าชีพจรเก่า, เป้าเล็งหมดอายุ, ขาวิ่งของ sprite
  setInterval(() => { runnerFrame++; schedule(); }, 350);
  setTab("live");
}

start();
