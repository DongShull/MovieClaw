import assert from "node:assert/strict";
import test from "node:test";

import { consumePlayIntent, markPlayIntent } from "../lib/player/play-links.ts";
import { StartupTrace } from "../lib/player/startup-trace.ts";

test("计时点相对起点、同名只记第一次、按时间排序交出", () => {
  const trace = new StartupTrace(1000);
  trace.mark("会话", 1160);
  trace.mark("进入", 1030);
  trace.mark("会话", 1900); // 重复到达：只认第一次
  trace.mark("播放", 1420);
  assert.equal(trace.finish(), null, "还没首帧不交");
  trace.mark("首帧", 1404);
  assert.deepEqual(trace.finish(), [
    { name: "进入", ms: 30 },
    { name: "会话", ms: 160 },
    { name: "首帧", ms: 404 },
    { name: "播放", ms: 420 },
  ]);
  assert.equal(trace.finish(), null, "每集只交一次");
});

test("迟迟没有首帧信号时开始播放后强制交出，缺首帧一项", () => {
  const trace = new StartupTrace(0);
  trace.mark("播放", 300);
  assert.deepEqual(trace.finish(true), [{ name: "播放", ms: 300 }]);
});

test("点播放的时刻只给紧接着的那次起播用一次", () => {
  markPlayIntent();
  const at = consumePlayIntent();
  assert.equal(typeof at, "number");
  assert.equal(consumePlayIntent(), null);
});
