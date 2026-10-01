import assert from "node:assert/strict";
import test from "node:test";

import { initialQoe, liveStats, reduceQoe, summarize } from "../lib/player/qoe.ts";

function run(events) {
  return summarize(events.reduce(reduceQoe, initialQoe()));
}

function runLive(events) {
  return liveStats(events.reduce(reduceQoe, initialQoe()));
}

test("首帧 = 点击播放到第一帧真正渲染", () => {
  const s = run([
    { type: "play-requested", at: 1000 },
    { type: "first-frame", at: 1850 },
  ]);
  assert.equal(s.ttff_ms, 850);
});

test("降档重来时的第二次出画不算首帧", () => {
  const s = run([
    { type: "play-requested", at: 0 },
    { type: "first-frame", at: 500 },
    { type: "first-frame", at: 9000 },
  ]);
  assert.equal(s.ttff_ms, 500);
});

test("开播后重开会话不覆盖起点——否则首帧早于起点、被夹成 0 上报", () => {
  const s = run([
    { type: "play-requested", at: 0 },
    { type: "first-frame", at: 500 },
    { type: "play-requested", at: 9000 }, // 换轨 / 按带宽重开
  ]);
  assert.equal(s.ttff_ms, 500);
});

test("起播前降档重来：首帧从第一次要地址算起（用户等的是整段）", () => {
  const s = run([
    { type: "play-requested", at: 0 },
    { type: "play-requested", at: 3000 }, // 这一档放不了，降档
    { type: "first-frame", at: 3800 },
  ]);
  assert.equal(s.ttff_ms, 3800);
});

test("起播请求后、首帧前的等待是起播本身，不算卡顿", () => {
  const s = run([
    { type: "play-requested", at: 0 },
    { type: "waiting", at: 100 }, // play() 先于数据调用，按规范先报 waiting
    { type: "first-frame", at: 400 },
    { type: "playing", at: 420 },
    { type: "waiting", at: 5000 }, // 开播之后的才是卡顿
    { type: "playing", at: 5600 },
  ]);
  assert.equal(s.rebuffer_count, 1);
  assert.equal(s.rebuffer_ms, 600);
});

test("没出画就没有首帧读数，不能填 0", () => {
  assert.equal(run([{ type: "play-requested", at: 0 }]).ttff_ms, null);
});

test("卡顿 = waiting 到 playing 的时长", () => {
  const s = run([
    { type: "waiting", at: 1000 },
    { type: "playing", at: 3500 },
  ]);
  assert.equal(s.rebuffer_ms, 2500);
  assert.equal(s.rebuffer_count, 1);
});

test("seek 引起的 waiting 不算卡顿——否则拖一下进度条就被记一次", () => {
  const s = run([
    { type: "seek-requested", at: 1000 },
    { type: "waiting", at: 1100 },
    { type: "playing", at: 2000 },
  ]);
  assert.equal(s.rebuffer_ms, 0);
  assert.equal(s.rebuffer_count, 0);
  assert.equal(s.seek_count, 1);
});

test("闸在「用户要求跳转」那一刻就开，不等 video 的 seeking 事件", () => {
  // 换会话那条路上元素的 seeking 一次都不会来（新流从自己时间轴的 0 秒起播，
  // hls.js 不 seek）。闸如果挂在元素事件上，这段等待就会被记成卡顿——
  // 「用户拖一下就多记一次卡顿」，正是这个模块要防的事。
  const s = run([
    { type: "seek-requested", at: 1000 },
    { type: "waiting", at: 1200 }, // 会话拆除 + 重开 + ffmpeg 起转
    { type: "playing", at: 9000 },
  ]);
  assert.equal(s.rebuffer_count, 0, "换会话的等待被记成了卡顿");
  assert.equal(s.seek_count, 1, "换会话那条路的跳转没被记下来");
});

test("元素的 seeking 只开闸、不计数——跟随写的每一次 currentTime 都会触发它", () => {
  const s = run([
    { type: "seeking", at: 1000 },
    { type: "seeking", at: 1100 },
    { type: "seeking", at: 1200 },
    { type: "waiting", at: 1300 },
    { type: "playing", at: 2000 },
  ]);
  assert.equal(s.seek_count, 0, "元素事件被当成了「用户跳了几次」");
  assert.equal(s.rebuffer_count, 0, "闸没开：seek 的等待被记成了卡顿");
});

test("seek 之后的真卡顿要照常记", () => {
  const s = run([
    { type: "seek-requested", at: 1000 },
    { type: "waiting", at: 1100 },
    { type: "playing", at: 2000 }, // seek 结束
    { type: "waiting", at: 5000 }, // 这次是真卡
    { type: "playing", at: 6000 },
  ]);
  assert.equal(s.rebuffer_ms, 1000);
  assert.equal(s.rebuffer_count, 1);
});

test("重复的 waiting 不叠加计数", () => {
  const s = run([
    { type: "waiting", at: 1000 },
    { type: "waiting", at: 1200 },
    { type: "playing", at: 2000 },
  ]);
  assert.equal(s.rebuffer_count, 1);
  assert.equal(s.rebuffer_ms, 1000);
});

test("没配对的 waiting 不计入（还没恢复就走了）", () => {
  const s = run([{ type: "waiting", at: 1000 }]);
  assert.equal(s.rebuffer_ms, 0);
  assert.equal(s.rebuffer_count, 0);
});

test("观看时长只累计真的在播的那部分", () => {
  const s = run([
    { type: "tick", at: 0, playing: true },
    { type: "tick", at: 1000, playing: true },
    { type: "tick", at: 2000, playing: false }, // 暂停的这一秒不算
    { type: "tick", at: 3000, playing: true },
  ]);
  assert.equal(s.watched_ms, 2000);
});

test("掉帧取最后一次读数", () => {
  const s = run([
    { type: "frames", dropped: 1, total: 100 },
    { type: "frames", dropped: 7, total: 900 },
  ]);
  assert.equal(s.dropped_frames, 7);
  assert.equal(s.total_frames, 900);
});

test("跳转耗时 = 用户要求跳转到画面恢复（playing）", () => {
  const live = runLive([
    { type: "seek-requested", at: 1000 },
    { type: "waiting", at: 1100 },
    { type: "playing", at: 2400 },
  ]);
  assert.equal(live.lastSeekMs, 1400);
  // seek 的等待不混进卡顿口径
  assert.equal(live.rebufferCount, 0);
});

test("连续拖拽以第一次为起点——用户的等待从第一下就开始了", () => {
  const live = runLive([
    { type: "seek-requested", at: 1000 },
    { type: "seek-requested", at: 1500 },
    { type: "seek-requested", at: 2000 },
    { type: "playing", at: 4000 },
  ]);
  assert.equal(live.lastSeekMs, 3000);
});

test("结算后再来一次 seek，重新起算", () => {
  const live = runLive([
    { type: "seek-requested", at: 1000 },
    { type: "playing", at: 1800 },
    { type: "seek-requested", at: 10000 },
    { type: "playing", at: 10200 },
  ]);
  assert.equal(live.lastSeekMs, 200);
});

test("没 seek 过就没有跳转读数；卡顿累计照常给", () => {
  const live = runLive([
    { type: "waiting", at: 1000 },
    { type: "playing", at: 3500 },
  ]);
  assert.equal(live.lastSeekMs, null);
  assert.equal(live.rebufferCount, 1);
  assert.equal(live.rebufferMs, 2500);
});
