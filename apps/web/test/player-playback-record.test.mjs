// 一次播放的记录（与 iOS 的 PlaybackRecordTests 同一口径）：只记原始事实，判定在服务端
import assert from "node:assert/strict";
import test from "node:test";

import { PlaybackRecord, parseServerTiming } from "../lib/player/playback-record.ts";
import {
  ACTIVE_STALE_MS,
  enqueueReport,
  flushReports,
  markActive,
  clearActive,
  recoverAbnormalExits,
} from "../lib/player/report-queue.ts";

/** 可以手动拨的单调时钟 */
function clock(start = 1000) {
  let t = start;
  return { now: () => t, advance: (ms) => (t += ms) };
}

const unit = { media_item_id: 6793 };

function record(c, extra = {}) {
  return new PlaybackRecord({ unit, origin: "tap", startedAt: c.now(), now: c.now, id: "a-1", ...extra });
}

function payload(r, extra = {}) {
  return r.payload({
    outcome: "exited",
    positionMs: 0,
    durationMs: null,
    watchedMs: 0,
    droppedFrames: null,
    totalFrames: null,
    context: {},
    ...extra,
  });
}

test("首帧与开播从点下算起，扣掉停在用户手里的等待（转码同意弹窗）", () => {
  const c = clock();
  const r = record(c);
  c.advance(300);
  r.beginUserWait();
  c.advance(5000);
  r.endUserWait();
  c.advance(200);
  r.noteFirstFrame();
  c.advance(100);
  r.notePlaying();
  const p = payload(r);
  assert.equal(p.first_frame_ms, 500);
  assert.equal(p.playing_ms, 600);
  assert.equal(p.user_wait_ms, 5000);
  assert.equal(p.client, "web");
  assert.equal(p.attempt_id, "a-1");
});

test("结局：没出画分失败 / 出画前退出；放到片尾附近算看完", () => {
  const c = clock();
  const r = record(c);
  assert.equal(r.outcome({ phaseIsError: true, phaseIsEnded: false, positionMs: 0, durationMs: null }), "failed");
  assert.equal(r.outcome({ phaseIsError: false, phaseIsEnded: false, positionMs: 0, durationMs: null }), "exit_before_start");
  r.noteFirstFrame();
  assert.equal(r.outcome({ phaseIsError: false, phaseIsEnded: false, positionMs: 60_000, durationMs: 7_200_000 }), "exited");
  assert.equal(r.outcome({ phaseIsError: false, phaseIsEnded: false, positionMs: 7_100_000, durationMs: 7_200_000 }), "watched");
  assert.equal(r.outcome({ phaseIsError: false, phaseIsEnded: true, positionMs: 0, durationMs: null }), "watched");
});

test("跳转：落地记耗时；没落地又跳一次记「被取代」；离开时还没落地记「放弃」", () => {
  const c = clock();
  const r = record(c);
  r.noteFirstFrame();
  r.beginSeek({ source: "button", fromMs: 600_000, toMs: 610_000, buffered: true, paused: false, restart: false });
  c.advance(120);
  assert.equal(r.seekPresented(), 120);
  r.beginSeek({ source: "scrub", fromMs: 610_000, toMs: 1_800_000, buffered: false, paused: false, restart: false });
  c.advance(900);
  r.beginSeek({ source: "scrub", fromMs: 1_800_000, toMs: 1_900_000, buffered: false, paused: false, restart: false });
  c.advance(2000);
  const seeks = payload(r).detail.seeks;
  assert.deepEqual(
    seeks.map((s) => [s.outcome, s.ms, s.buffered]),
    [
      ["landed", 120, true],
      ["superseded", 900, false],
      ["abandoned", 2000, false],
    ],
  );
  assert.equal(payload(r).seek_count, 3);
});

test("连续拖动以第一次拖动为起点", () => {
  const c = clock();
  const r = record(c);
  r.noteFirstFrame();
  r.noteScrubActivity();
  c.advance(400);
  r.noteScrubActivity();
  r.beginSeek({ source: "scrub", fromMs: 0, toMs: 100_000, buffered: false, paused: false, restart: false });
  c.advance(600);
  assert.equal(r.seekPresented(), 1000);
});

test("换会话式的跳转在新流首帧时落地", () => {
  const c = clock();
  const r = record(c);
  r.noteFirstFrame();
  r.beginSeek({ source: "button", fromMs: 0, toMs: 3_000_000, buffered: false, paused: false, restart: true });
  c.advance(1700);
  r.noteFirstFrame();
  const [seek] = payload(r).detail.seeks;
  assert.equal(seek.outcome, "landed");
  assert.equal(seek.ms, 1700);
});

test("卡顿：用户想看、画面已出、没在跳转时播放头 0.5 秒不走；起播与跳转的等待不算", () => {
  const c = clock();
  const r = record(c);
  // 起播前播放头不动不算
  for (let i = 0; i < 8; i += 1) {
    r.samplePlayhead(0, true, () => "x");
    c.advance(250);
  }
  r.noteFirstFrame();
  // 首帧后有 1.5 秒起步宽限
  let pos = 0;
  for (let i = 0; i < 8; i += 1) {
    pos += 250;
    r.samplePlayhead(pos, true, () => "x");
    c.advance(250);
  }
  // 播放头停 1 秒
  for (let i = 0; i < 5; i += 1) {
    r.samplePlayhead(pos, true, () => "buffer_empty");
    c.advance(250);
  }
  pos += 250;
  r.samplePlayhead(pos, true, () => "x");
  const p = payload(r);
  assert.equal(p.rebuffer_count, 1);
  // 从最后一次看到播放头在走算起：停了 5 个采样（1.25 秒）再加上最后一次前进之后的那 250 毫秒
  assert.equal(p.rebuffer_ms, 1500);
  assert.equal(p.detail.interruptions[0].cause, "buffer_empty");
});

test("重连：从断线到新流首帧记一次 reconnect；离开时还没接回来照样记上", () => {
  const c = clock();
  const r = record(c);
  r.noteFirstFrame();
  r.beginReconnect("连续 15 秒没有收到数据");
  c.advance(3200);
  r.noteFirstFrame();
  r.beginReconnect("又断了");
  c.advance(800);
  const kinds = payload(r).detail.interruptions.map((i) => [i.kind, i.ms]);
  assert.deepEqual(kinds, [
    ["reconnect", 3200],
    ["reconnect", 800],
  ]);
});

test("首帧后 30 秒内第一次换音轨 / 字幕算猜错；续播后马上远跳算续播位置猜错", () => {
  const c = clock();
  const r = record(c);
  r.resumed = true;
  r.noteFirstFrame();
  c.advance(5000);
  r.noteAudioChange("embedded:1", "embedded:2");
  r.noteAudioChange("embedded:2", "embedded:1");
  r.beginSeek({ source: "scrub", fromMs: 600_000, toMs: 60_000, buffered: false, paused: false, restart: false });
  c.advance(40_000);
  r.noteSubtitleChange(null, "external:a.srt");
  const behaviors = payload(r).detail.behaviors.map((b) => [b.kind, b.misguess]);
  assert.deepEqual(behaviors, [
    ["audio_change", true],
    ["audio_change", false],
    ["resume_seek", true],
    ["subtitle_change", false],
  ]);
});

test("Server-Timing 头与 Resource Timing 两种来源都认", () => {
  assert.deepEqual(parseServerTiming("total;dur=729, decide;dur=330, prep;dur=0, spawn;dur=393"), {
    total: 729,
    decide: 330,
    prep: 0,
    spawn: 393,
  });
  assert.deepEqual(parseServerTiming([{ name: "total", duration: 52.4 }]), { total: 52 });
  assert.deepEqual(parseServerTiming(null), {});
});

/** 内存版存储 */
function memoryStorage() {
  const map = new Map();
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    keys: () => [...map.keys()],
  };
}

const p1 = { attempt_id: "a-1", outcome: "exited" };
const p2 = { attempt_id: "a-2", outcome: "failed" };

test("队列：发成功的删掉，遇到失败就停，留到下次补发", async () => {
  const storage = memoryStorage();
  enqueueReport(storage, p1, 0);
  enqueueReport(storage, p2, 1);
  let calls = 0;
  const sent = await flushReports(
    storage,
    async (p) => {
      calls += 1;
      if (p.attempt_id === "a-2") throw new Error("offline");
    },
    2,
  );
  assert.equal(sent, 1);
  assert.equal(calls, 2);
  const rest = JSON.parse(storage.getItem("movieclaw.player.report-queue"));
  assert.deepEqual(rest.map((r) => r.payload.attempt_id), ["a-2"]);
});

test("队列：同一编号只留最新一份；发送途中进队的新记录不会被删掉", async () => {
  const storage = memoryStorage();
  enqueueReport(storage, p1, 0);
  enqueueReport(storage, { ...p1, outcome: "watched" }, 1);
  const sent = await flushReports(
    storage,
    async () => {
      enqueueReport(storage, p2, 5);
    },
    2,
  );
  assert.equal(sent, 1);
  const rest = JSON.parse(storage.getItem("movieclaw.player.report-queue"));
  assert.deepEqual(rest.map((r) => r.payload.attempt_id), ["a-2"]);
});

test("异常退出：标记 30 秒没刷新才补报（别的标签页还在播的不动），正常离开删掉标记", () => {
  const storage = memoryStorage();
  markActive(storage, p1, 0);
  markActive(storage, p2, 0);
  clearActive(storage, "a-2");
  assert.equal(recoverAbnormalExits(storage, ACTIVE_STALE_MS - 1), 0);
  assert.equal(recoverAbnormalExits(storage, ACTIVE_STALE_MS + 1), 1);
  const queue = JSON.parse(storage.getItem("movieclaw.player.report-queue"));
  assert.equal(queue[0].payload.attempt_id, "a-1");
  assert.equal(queue[0].payload.outcome, "abnormal_exit");
  assert.equal(storage.keys().some((k) => k.startsWith("movieclaw.player.active.")), false);
});
