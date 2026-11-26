// ===== Dashboard logic: MQTT (WebSocket) + Grafana iframes + demo mode =====
const $ = (id) => document.getElementById(id);
const T = CONFIG.TOPICS;
const G = CONFIG.GRAFANA;

const RUNNING = ['GREEN_LIGHT', 'TRANSITION', 'RED_LIGHT'];

const S = {
  broker: false,
  game: { state: 'IDLE', remaining: null, recvAt: 0, reason: null, part: null, mode: 'auto' },
  devices: { esp1: 'unknown', esp2: 'unknown', pi: 'unknown' },
  rounds: [],
  topics: new Map()
};

let client = null;
let soundOn = false;
let audioCtx = null;

// ---------------------------------------------------------------- helpers
function parse(raw) {
  try { return JSON.parse(raw); } catch { return raw; }
}

function send(topic, obj) {
  if (CONFIG.DEMO) return demoCommand(topic, obj);
  if (client && client.connected) client.publish(topic, JSON.stringify(obj), { qos: 1 });
}

function setDot(id, ok) {
  const el = $(id);
  el.classList.toggle('on', ok === true);
  el.classList.toggle('off', ok === false);
}

// ---------------------------------------------------------------- sound
function beep(seq) {
  if (!soundOn) return;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  let t = audioCtx.currentTime;
  seq.forEach(([freq, dur]) => {
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.frequency.value = freq;
    o.connect(g); g.connect(audioCtx.destination);
    g.gain.setValueAtTime(0.15, t);
    o.start(t); o.stop(t + dur);
    t += dur;
  });
}
const SND_WIN = [[523, 0.15], [659, 0.15], [784, 0.35]];
const SND_LOSE = [[220, 0.3], [160, 0.5]];

$('btn-sound').onclick = () => {
  soundOn = !soundOn;
  $('btn-sound').textContent = soundOn ? '🔊 ปิดเสียง' : '🔇 เปิดเสียง';
  if (soundOn) beep([[660, 0.1]]);
};

// ---------------------------------------------------------------- zone 1: game display
const LABELS = {
  IDLE: ['IDLE / CALIBRATE', 'รอเริ่มเกม'],
  GREEN_LIGHT: ['GREEN LIGHT', 'เดินได้'],
  TRANSITION: ['GRACE PERIOD', 'เตรียมหยุด (ยังไม่จับแพ้)'],
  RED_LIGHT: ['RED LIGHT', 'ห้ามขยับ'],
  WIN: ['WIN', 'ชนะแล้ว!'],
  GAME_OVER: ['GAME OVER', 'แพ้แล้ว'],
  PAUSED: ['PAUSED', 'เกมหยุดชั่วคราว']
};
const REASONS = {
  gyro: 'ขยับมือ (Gyro เกิน Threshold)',
  camera: 'ขยับตัว (กล้องจับการขยับ)',
  touch_red: 'แตะเส้นชัยตอนไฟแดง'
};

function onState(d) {
  if (typeof d !== 'object' || !d) return;
  const prev = S.game.state;
  const g = S.game;
  g.state = d.state || g.state;
  g.reason = d.reason || null;
  g.part = d.part || null;
  if (d.mode) g.mode = d.mode;
  g.remaining = typeof d.remaining_s === 'number' ? d.remaining_s : null;
  g.recvAt = Date.now();

  if (g.state !== prev) {
    if (g.state === 'WIN') beep(SND_WIN);
    if (g.state === 'GAME_OVER') beep(SND_LOSE);
    if (g.state === 'GREEN_LIGHT' && !RUNNING.includes(prev)) {
      $('ai-box').textContent = 'รอจบเกม…';
      $('ai-badge').classList.add('hidden');
    }
  }
  renderGame();
  renderAlert();
  renderControls();
}

function renderGame() {
  const g = S.game;
  const [title, sub] = LABELS[g.state] || [g.state, ''];
  $('banner').className = 'banner s-' + g.state;
  $('banner-state').textContent = title;
  $('banner-sub').textContent = sub;

  const box = $('reason-box');
  if (g.state === 'GAME_OVER') {
    let txt = 'สาเหตุที่แพ้: ' + (REASONS[g.reason] || g.reason || 'ไม่ระบุ');
    if (g.part) txt += ` (${g.part})`;
    box.textContent = txt;
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
  }
}

// นับถอยหลังฝั่งเว็บเองจาก remaining_s ที่ได้รับล่าสุด (ไม่พึ่งนาฬิกาของเครื่องอื่น)
setInterval(() => {
  const g = S.game;
  if (g.remaining == null) { $('countdown').textContent = '--'; return; }
  const elapsed = g.state === 'PAUSED' ? 0 : (Date.now() - g.recvAt) / 1000;
  $('countdown').textContent = Math.max(0, g.remaining - elapsed).toFixed(1);
}, 100);

function onDistance(d) {
  const cm = typeof d === 'object' ? d.cm : Number(d);
  if (!isFinite(cm)) return;
  const span = CONFIG.START_DISTANCE_CM - CONFIG.FINISH_DISTANCE_CM;
  const pct = Math.min(100, Math.max(0, ((CONFIG.START_DISTANCE_CM - cm) / span) * 100));
  $('dist-bar').style.width = pct + '%';
  $('dist-text').textContent = `${Math.round(cm)} cm (${Math.round(pct)}%)`;
}

function onMotion(d) {
  if (typeof d !== 'object' || !d) return;
  if (typeof d.fps === 'number') $('fps').textContent = d.fps.toFixed(1);
  $('motion').textContent = d.moved
    ? `ขยับ${d.part ? ' (' + d.part + ')' : ''}${typeof d.score === 'number' ? ' ' + d.score.toFixed(2) : ''}`
    : 'นิ่ง';
}

// วิดีโอ: ตั้ง src ครั้งเดียว ถ้า error ให้ลองใหม่ทุก 3 วินาที
function initVideo() {
  const img = $('video');
  const msg = $('video-msg');
  const load = () => { img.src = CONFIG.PI_VIDEO_URL + (CONFIG.PI_VIDEO_URL.includes('?') ? '&' : '?') + 't=' + Date.now(); };
  img.onload = () => msg.classList.add('hidden');
  img.onerror = () => { msg.classList.remove('hidden'); setTimeout(load, 3000); };
  if (!CONFIG.DEMO) load();
}

// ---------------------------------------------------------------- zone 4: control + status
function onDeviceStatus(dev, data) {
  const v = typeof data === 'string' ? data : (data && data.status);
  S.devices[dev] = v === 'online' ? 'online' : 'offline';
  setDot('dot-' + dev, S.devices[dev] === 'online');
  renderAlert();
}

function setBroker(ok) {
  S.broker = ok;
  setDot('dot-broker', ok);
  setDot('dot-broker2', ok);
  renderControls();
  renderAlert();
}

function renderControls() {
  const running = RUNNING.includes(S.game.state);
  $('btn-start').disabled = !S.broker || running;
  $('btn-calibrate').disabled = !S.broker || running;
  $('btn-reset').disabled = !S.broker;
  document.querySelectorAll('input[name=servo]').forEach((r) => {
    r.disabled = !S.broker;
    r.checked = r.value === S.game.mode;
  });
}

$('btn-start').onclick = () => send(T.cmdGame, { cmd: 'start' });
$('btn-reset').onclick = () => send(T.cmdGame, { cmd: 'reset' });
$('btn-calibrate').onclick = () => send(T.cmdGame, { cmd: 'calibrate' });
document.querySelectorAll('input[name=servo]').forEach((r) => {
  r.onchange = () => { S.game.mode = r.value; send(T.cmdServo, { mode: r.value }); };
});

function onCalibration(d) {
  if (typeof d !== 'object' || !d) return;
  const bpm = d.bpm_baseline != null ? Number(d.bpm_baseline).toFixed(0) : '--';
  const gyro = d.gyro_baseline != null ? Number(d.gyro_baseline).toFixed(2) : '--';
  $('calib').textContent = `BPM baseline: ${bpm} · Gyro baseline: ${gyro}`;
}

// แจ้งเตือนอุปกรณ์หลุด / เกม Pause
function renderAlert() {
  const el = $('alert');
  const offline = CONFIG.DEVICES.filter((d) => S.devices[d] === 'offline');
  const playing = RUNNING.includes(S.game.state) || S.game.state === 'PAUSED';
  let msg = '', danger = false;
  if (!S.broker) {
    msg = '⚠️ เว็บต่อ MQTT Broker ไม่ได้ (ข้อมูล Real-time จะไม่อัปเดต)';
    danger = true;
  } else if (offline.length && playing) {
    msg = `⛔ อุปกรณ์หลุดกลางเกม: ${offline.join(', ').toUpperCase()} — เกมถูก Pause`;
    danger = true;
  } else if (offline.length) {
    msg = `⚠️ อุปกรณ์ Offline: ${offline.join(', ').toUpperCase()}`;
  }
  el.textContent = msg;
  el.classList.toggle('hidden', !msg);
  el.classList.toggle('danger', danger);
}

// ---------------------------------------------------------------- zone 2 + 3: Grafana
function panelUrl(id, from, to, refresh) {
  const q = new URLSearchParams({ orgId: 1, panelId: id, from, to, theme: 'light' });
  if (refresh) q.set('refresh', refresh);
  return `${G.BASE}/d-solo/${G.UID}/${G.SLUG}?${q}`;
}
function setFrame(id, url) {
  const f = $(id);
  if (f.dataset.url !== url) { f.dataset.url = url; f.src = url; }
}

function renderGrafana() {
  // โซน 2: real-time
  setFrame('gf-bpm', panelUrl(G.PANELS.bpm, 'now-30s', 'now', '1s'));
  setFrame('gf-gyro', panelUrl(G.PANELS.gyro, 'now-30s', 'now', '1s'));

  // โซน 3: ย้อนหลังตามรอบที่เลือก
  const sel = $('round-select').value;
  let from = 'now-5m', to = 'now', refresh = '5s';
  if (sel !== 'live') {
    const r = S.rounds.find((x) => String(x.id) === sel);
    if (r) { from = r.start_ms; to = r.end_ms || 'now'; refresh = r.end_ms ? '' : '5s'; }
  }
  setFrame('gf-hbpm', panelUrl(G.PANELS.historyBpm, from, to, refresh));
  setFrame('gf-hgyro', panelUrl(G.PANELS.historyGyro, from, to, refresh));
  setFrame('gf-board', panelUrl(G.PANELS.leaderboard, 'now-7d', 'now', '10s'));
}

function onRounds(d) {
  if (!Array.isArray(d)) return;
  S.rounds = d;
  const sel = $('round-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="live">Live (5 นาทีล่าสุด)</option>';
  [...d].reverse().forEach((r) => {
    const o = document.createElement('option');
    o.value = r.id;
    const t = new Date(r.start_ms).toLocaleTimeString('th-TH');
    o.textContent = `รอบ #${r.id} · ${t} · ${r.result || 'กำลังเล่น'}`;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  renderGrafana();
}
$('round-select').onchange = renderGrafana;

// ---------------------------------------------------------------- AI + Evaluation
function onCommentary(d) {
  if (!d) return;
  const text = typeof d === 'object' ? d.text : String(d);
  const src = typeof d === 'object' ? d.source : null;
  $('ai-box').textContent = text;
  const b = $('ai-badge');
  if (src) {
    b.textContent = src === 'fallback' ? 'ข้อความสำรอง (AI ไม่ตอบ)' : src;
    b.classList.remove('hidden');
  } else b.classList.add('hidden');
}

function onEval(d) {
  if (typeof d !== 'object' || !d) return;
  if (d.fps_avg != null) $('ev-fps').textContent = Number(d.fps_avg).toFixed(1);
  if (d.accuracy != null) $('ev-acc').textContent = (Number(d.accuracy) * 100).toFixed(1) + '%';
  if (d.n != null) $('ev-n').textContent = d.n;
  if ('tp' in d) $('ev-cm').textContent = `${d.tp} / ${d.fn} / ${d.tn} / ${d.fp}`;
}
$('ev-link').href = CONFIG.PI_CAPTURES_URL;

// ---------------------------------------------------------------- topic monitor
let monitorDirty = false;
function monitor(topic, raw) {
  const e = S.topics.get(topic) || { count: 0 };
  e.payload = String(raw).slice(0, 160);
  e.ts = new Date();
  e.count++;
  S.topics.set(topic, e);
  monitorDirty = true;
}
setInterval(() => {
  if (!monitorDirty) return;
  monitorDirty = false;
  const body = document.querySelector('#topic-table tbody');
  body.innerHTML = '';
  [...S.topics.keys()].sort().forEach((k) => {
    const e = S.topics.get(k);
    const tr = document.createElement('tr');
    [k, e.payload, e.ts.toLocaleTimeString('th-TH'), e.count].forEach((v, i) => {
      const td = document.createElement('td');
      td.textContent = v;
      if (i === 1) td.className = 'payload';
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}, 500);

// ---------------------------------------------------------------- message router
function handle(topic, raw) {
  const data = parse(raw);
  monitor(topic, raw);
  if (topic === T.state) onState(data);
  else if (topic === T.calibration) onCalibration(data);
  else if (topic === T.rounds) onRounds(data);
  else if (topic === T.distance) onDistance(data);
  else if (topic === T.motion) onMotion(data);
  else if (topic === T.eval) onEval(data);
  else if (topic === T.commentary) onCommentary(data);
  else if (topic.startsWith(T.statusPrefix)) {
    const dev = topic.slice(T.statusPrefix.length);
    if (CONFIG.DEVICES.includes(dev)) onDeviceStatus(dev, data);
  }
}

// ---------------------------------------------------------------- MQTT connection
function connect() {
  client = mqtt.connect(CONFIG.MQTT_WS_URL, {
    clientId: 'rlgl-web-' + Math.random().toString(16).slice(2, 8),
    reconnectPeriod: 2000,
    clean: true
  });
  client.on('connect', () => { setBroker(true); client.subscribe('rlgl/#', { qos: 0 }); });
  client.on('close', () => setBroker(false));
  client.on('error', (e) => console.warn('MQTT error', e.message));
  client.on('message', (topic, payload) => handle(topic, payload.toString()));
}

// ---------------------------------------------------------------- demo mode (?demo=1)
// จำลองข้อความที่ Game Engine / Pi / ESP จะส่งมา เพื่อลองหน้าตาโดยไม่ต้องมี broker
const demo = { timer: null, rounds: [] };
const pub = (topic, obj) => handle(topic, typeof obj === 'string' ? obj : JSON.stringify(obj));

function demoStart() {
  $('demo-tag').classList.remove('hidden');
  setBroker(true);
  CONFIG.DEVICES.forEach((d) => pub(T.statusPrefix + d, 'online'));
  pub(T.state, { state: 'IDLE', mode: 'auto' });
  pub(T.eval, { fps_avg: 14.2, accuracy: 0.925, n: 40, tp: 18, fn: 2, tn: 19, fp: 1 });
  pub(T.distance, { cm: CONFIG.START_DISTANCE_CM });
  pub(T.motion, { moved: false, fps: 14.0 });
}

function demoStop() { clearInterval(demo.timer); demo.timer = null; }

function demoCommand(topic, obj) {
  if (topic === T.cmdServo) pub(T.state, { state: S.game.state, mode: obj.mode, remaining_s: S.game.remaining });
  if (topic !== T.cmdGame) return;
  if (obj.cmd === 'reset') {
    demoStop();
    pub(T.state, { state: 'IDLE', mode: S.game.mode });
    pub(T.distance, { cm: CONFIG.START_DISTANCE_CM });
  }
  if (obj.cmd === 'calibrate') {
    pub(T.calibration, { bpm_baseline: 72 + Math.random() * 6, gyro_baseline: 0.05 + Math.random() * 0.03 });
  }
  if (obj.cmd === 'start') demoRun();
}

function demoRun() {
  demoStop();
  const round = { id: demo.rounds.length + 1, start_ms: Date.now(), end_ms: null, result: null };
  demo.rounds.push(round);
  pub(T.rounds, demo.rounds);

  let dist = CONFIG.START_DISTANCE_CM;
  let phase = 'GREEN_LIGHT';
  let left = 4 + Math.random() * 3;
  const emit = (st, extra = {}) => pub(T.state, { state: st, remaining_s: left, round_id: round.id, mode: S.game.mode, ...extra });

  const finish = (result, extra = {}) => {
    demoStop();
    round.end_ms = Date.now();
    round.result = result === 'WIN' ? 'ชนะ' : 'แพ้';
    pub(T.rounds, demo.rounds);
    pub(T.state, { state: result, round_id: round.id, mode: S.game.mode, ...extra });
    setTimeout(() => pub(T.commentary, {
      source: 'fallback',
      text: result === 'WIN' ? 'ชนะแล้ว! หัวใจเต้นแรงแต่มือนิ่งสุดๆ' : 'ขยับจนได้ ไฟแดงไม่ปรานีใคร ลองใหม่อีกรอบนะ'
    }), 1000);
  };

  emit(phase);
  demo.timer = setInterval(() => {
    left -= 0.2;
    if (phase === 'GREEN_LIGHT') {
      dist -= 10 + Math.random() * 10;
      pub(T.distance, { cm: Math.max(dist, 0) });
      pub(T.motion, { moved: false, fps: 12 + Math.random() * 4 });
      if (dist <= CONFIG.FINISH_DISTANCE_CM) return finish('WIN');
      if (left <= 0) { phase = 'TRANSITION'; left = 0.8; emit(phase); }
    } else if (phase === 'TRANSITION') {
      if (left <= 0) { phase = 'RED_LIGHT'; left = 3; emit(phase); }
    } else if (phase === 'RED_LIGHT') {
      const moved = Math.random() < 0.04;
      pub(T.motion, { moved, part: moved ? 'left_arm' : null, score: moved ? 0.08 : 0.01, fps: 12 + Math.random() * 4 });
      if (moved) return finish('GAME_OVER', { reason: Math.random() < 0.5 ? 'camera' : 'gyro', part: 'left_arm' });
      if (left <= 0) { phase = 'GREEN_LIGHT'; left = 4 + Math.random() * 3; emit(phase); }
    }
  }, 200);
}

// ---------------------------------------------------------------- init
renderGame();
renderControls();
renderAlert();
renderGrafana();
initVideo();
if (CONFIG.DEMO) demoStart(); else connect();
