// เทสต์ส่วนคำนวณของหน้าเว็บ (ไม่ต้องใช้ browser)
// รันจากโฟลเดอร์ laptop/web:  node --test tests/frontend/
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  clock, countdown, screenFor, causeText, partName, progress, bpmFresh, Recorder, whatHappened, roundLabel,
} from "../../frontend/js/logic.js";

test("clock shows minutes, seconds and tenths", () => {
  assert.equal(clock(17.94), "00:17.9");
  assert.equal(clock(61.25), "01:01.3");
  assert.equal(clock(0), "00:00.0");
  assert.equal(clock(null), "--:--.-");
  assert.equal(clock(59.97), "01:00.0"); // ปัดแล้วต้องไม่เป็น 00:60.0
});

test("countdown shows whole seconds and never goes below zero", () => {
  assert.equal(countdown(42), "00:42");
  assert.equal(countdown(60), "01:00");
  assert.equal(countdown(-3), "00:00");
  assert.equal(countdown(undefined), "--:--");
});

test("round label pads the number", () => {
  assert.equal(roundLabel(7), "#07");
  assert.equal(roundLabel(124), "#124");
  assert.equal(roundLabel(null), "#--");
});

test("the screen follows the game state", () => {
  const base = { connected: true, roundId: "r1", dismissedRound: null };
  assert.equal(screenFor({ ...base, state: undefined }), "setup");
  assert.equal(screenFor({ ...base, state: "IDLE" }), "setup");
  assert.equal(screenFor({ ...base, state: "CALIBRATE" }), "calibrate");
  for (const state of ["GREEN_LIGHT", "TRANSITION", "RED_LIGHT"]) {
    assert.equal(screenFor({ ...base, state }), "play", state);
  }
  assert.equal(screenFor({ ...base, state: "SNIPER" }), "sniper");
  assert.equal(screenFor({ ...base, state: "WIN" }), "result");
  assert.equal(screenFor({ ...base, state: "GAME_OVER" }), "result");
});

test("a dismissed result goes back to the name screen only for that round", () => {
  const base = { connected: true, state: "GAME_OVER", roundId: "r1" };
  assert.equal(screenFor({ ...base, dismissedRound: "r1" }), "setup");
  assert.equal(screenFor({ ...base, dismissedRound: "r0" }), "result");
});

test("a lost connection always wins over the last colour", () => {
  assert.equal(screenFor({ connected: false, state: "GREEN_LIGHT", roundId: "r1" }), "offline");
  assert.equal(screenFor({ connected: false, state: "RED_LIGHT", roundId: "r1" }), "offline");
});

test("body part names are readable", () => {
  assert.equal(partName("right_arm"), "right arm");
  assert.equal(partName("head"), "head");
  assert.equal(partName(null), null);
});

test("cause text explains every result", () => {
  assert.equal(causeText({ result: "WIN", reason: "touch", finish_state: "GREEN_LIGHT" }),
    "Touched finish during GREEN");
  assert.equal(causeText({ result: "WIN", reason: "touch", finish_state: "TRANSITION" }),
    "Touched finish during TRANSITION");
  assert.equal(causeText({ result: "GAME_OVER", reason: "camera", caught_part: "right_arm", caught_red_s: 3.8 }),
    "Moved on RED · right arm (camera) · 3.8 s into red");
  assert.equal(causeText({ result: "GAME_OVER", reason: "camera" }), "Moved on RED · seen by the camera");
  assert.equal(causeText({ result: "GAME_OVER", reason: "imu" }), "Moved on RED · wrist IMU over threshold");
  assert.equal(causeText({ result: "GAME_OVER", reason: "both" }), "Moved on RED · camera and wrist IMU");
  assert.equal(causeText({ result: "GAME_OVER", reason: "timeout" }), "Time out before reaching the finish");
  assert.equal(causeText({ result: "GAME_OVER", reason: "finish_during_red" }),
    "Touched the finish sensor during RED LIGHT");
  assert.equal(causeText({ result: "ABORTED", reason: "disconnect" }), "Round cancelled · a device went offline");
  assert.equal(causeText({ result: "ABORTED", reason: "camera_lost" }), "Round cancelled · camera lost the player");
});

test("cause text for sniper rounds", () => {
  assert.equal(causeText({ mode: "sniper", result: "WIN", reason: "touch", hits: 1 }),
    "Pressed the button · 1 hit taken");
  assert.equal(causeText({ mode: "sniper", result: "WIN", reason: "touch", hits: 0 }),
    "Pressed the button · no hits");
  assert.equal(causeText({ mode: "sniper", result: "GAME_OVER", reason: "hits", hits: 3 }),
    "Hit 3 times by the sniper");
  assert.equal(causeText({ mode: "sniper", result: "GAME_OVER", reason: "timeout" }),
    "Time out before pressing the button");
});

test("progress turns the ultrasonic distance into metres walked", () => {
  assert.deepEqual(progress(110, 300), { covered_m: 1.9, total_m: 3.0, ratio: 190 / 300 });
  assert.deepEqual(progress(400, 300), { covered_m: 0, total_m: 3.0, ratio: 0 });
  assert.deepEqual(progress(-5, 300), { covered_m: 3.0, total_m: 3.0, ratio: 1 });
  assert.equal(progress(undefined, 300), null);
});

test("heart rate is stale after 10 seconds", () => {
  assert.equal(bpmFresh(1000, 10_999), true);
  assert.equal(bpmFresh(1000, 11_001), false);
  assert.equal(bpmFresh(null, 5000), false);
});

// ---------- บันทึกรอบระหว่างเล่น (ใช้ทำ timeline หน้าผล) ----------

function feed(recorder, steps) {
  for (const [ms, topic, payload] of steps) recorder.message(topic, payload, ms);
}

const S = "rlgl/game/state";

test("the recorder builds light segments from the first playing state", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "r1", state: "CALIBRATE" }],
    [5000, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [5500, S, { round_id: "r1", state: "GREEN_LIGHT" }], // ส่งซ้ำทุกวินาที ไม่ใช่ช่วงใหม่
    [8000, S, { round_id: "r1", state: "TRANSITION" }],
    [9000, S, { round_id: "r1", state: "RED_LIGHT" }],
    [12000, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [15000, S, { round_id: "r1", state: "WIN" }],
  ]);
  const t = rec.timeline();
  assert.equal(t.round_id, "r1");
  assert.deepEqual(t.segments, [
    { state: "GREEN_LIGHT", from: 0, to: 3 },
    { state: "TRANSITION", from: 3, to: 4 },
    { state: "RED_LIGHT", from: 4, to: 7 },
    { state: "GREEN_LIGHT", from: 7, to: 10 },
  ]);
  assert.equal(t.end, 10);
});

test("the recorder keeps samples with their time and drops the calibration ones", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "r1", state: "CALIBRATE" }],
    [1000, "rlgl/wrist/heartrate", { bpm: 76 }],
    [2000, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [2500, "rlgl/wrist/heartrate", { bpm: 98 }],
    [3000, "rlgl/gateway/distance", { distance_cm: 250 }],
    [4000, S, { round_id: "r1", state: "RED_LIGHT" }],
    [4500, "rlgl/gateway/vision", { score_ratio: 0.4, motion_score: 0.02 }],
    [4600, "rlgl/gateway/vision", { fps: 18 }], // ไม่มีคะแนน ไม่เก็บ
  ]);
  const t = rec.timeline();
  assert.deepEqual(t.bpm, [{ t: 0.5, v: 98 }]);
  assert.deepEqual(t.distance, [{ t: 1, cm: 250 }]);
  assert.deepEqual(t.motion, [{ t: 2.5, v: 0.4 }]);
  assert.equal(t.bpm_rest, 76);
});

test("a new round id starts a fresh recording", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [1000, "rlgl/wrist/heartrate", { bpm: 90 }],
    [5000, S, { round_id: "r2", state: "CALIBRATE" }],
  ]);
  const t = rec.timeline();
  assert.equal(t.round_id, "r2");
  assert.deepEqual(t.bpm, []);
  assert.deepEqual(t.segments, []);
});

test("what happened lists each red light and the ending", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [1000, "rlgl/gateway/distance", { distance_cm: 280 }],
    [3000, S, { round_id: "r1", state: "RED_LIGHT" }],
    [3500, "rlgl/gateway/distance", { distance_cm: 210 }],
    [3600, "rlgl/gateway/vision", { score_ratio: 0.3 }],
    [6000, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [8000, S, { round_id: "r1", state: "RED_LIGHT" }],
    [8200, "rlgl/gateway/vision", { score_ratio: 0.82 }],
    [10000, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [11000, "rlgl/gateway/distance", { distance_cm: 50 }],
    [12000, "rlgl/game/result", { round_id: "r1", result: "WIN", reason: "touch" }],
  ]);
  const rows = whatHappened(rec.timeline(), 300);
  assert.deepEqual(rows.map((r) => [r.t, r.kind, r.title]), [
    [0, "start", "Start"],
    [3, "ok", "Red light 1 - survived"],
    [8, "close", "Red light 2 - close call"],
    [12, "ok", "Touched the finish"],
  ]);
  assert.equal(rows[1].detail, "Froze at 0.9 m");
  assert.equal(rows[2].detail, "Max motion 0.82 at 0.9 m");
});

test("what happened marks the red light where the player was caught", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "r1", state: "GREEN_LIGHT" }],
    [3000, S, { round_id: "r1", state: "RED_LIGHT" }],
    [4500, "rlgl/gateway/vision", { score_ratio: 2.4 }],
    [5000, "rlgl/game/result", { round_id: "r1", result: "GAME_OVER", reason: "camera", caught_part: "left_leg" }],
    [5100, S, { round_id: "r1", state: "GAME_OVER" }],
  ]);
  const rows = whatHappened(rec.timeline(), 300);
  assert.deepEqual(rows.map((r) => [r.t, r.kind, r.title]), [
    [0, "start", "Start"],
    [3, "bad", "Red light 1 - moved"],
    [5, "bad", "Caught"],
  ]);
  assert.equal(rows[2].detail, "Moved on RED · left leg (camera)");
});

test("sniper rounds list each hit", () => {
  const rec = new Recorder();
  feed(rec, [
    [0, S, { round_id: "s1", mode: "sniper", state: "SNIPER", lives: 3, hits: 0 }],
    [1000, S, { round_id: "s1", mode: "sniper", state: "SNIPER", lives: 3, hits: 0 }],
    [4200, S, { round_id: "s1", mode: "sniper", state: "SNIPER", lives: 2, hits: 1 }],
    [5000, S, { round_id: "s1", mode: "sniper", state: "SNIPER", lives: 2, hits: 1 }],
    [9000, "rlgl/game/result", { round_id: "s1", mode: "sniper", result: "WIN", reason: "touch", hits: 1 }],
  ]);
  const t = rec.timeline();
  assert.equal(t.mode, "sniper");
  assert.deepEqual(t.hits, [{ t: 4.2, lives: 2 }]);
  const rows = whatHappened(t, 300);
  assert.deepEqual(rows.map((r) => [r.t, r.kind, r.title]), [
    [0, "start", "Start"],
    [4.2, "bad", "Hit 1 - 2 lives left"],
    [9, "ok", "Pressed the button"],
  ]);
});
