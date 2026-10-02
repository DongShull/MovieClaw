import assert from "node:assert/strict";
import test from "node:test";

import { parseSegmentStarts, snapToKeyframe } from "../lib/player/keyframe-snap.ts";

const PLAYLIST = [
  "#EXTM3U",
  "#EXT-X-TARGETDURATION:10",
  "#EXT-X-MAP:URI=\"init.mp4?token=t\"",
  "#EXTINF:10.000000,",
  "seg00000.m4s?token=t",
  "#EXTINF:5.800000,",
  "seg00001.m4s?token=t",
  "#EXTINF:5.800000,",
  "seg00002.m4s?token=t",
  "#EXTINF:2.483000,",
  "seg00003.m4s?token=t",
  "#EXT-X-ENDLIST",
].join("\n");

test("媒体列表的 EXTINF 累加就是每段起点（关键帧）", () => {
  const starts = parseSegmentStarts(PLAYLIST);
  assert.equal(starts.length, 4);
  assert.deepEqual(starts.map((s) => Number(s.toFixed(3))), [0, 10, 15.8, 21.6]);
  assert.deepEqual(parseSegmentStarts("#EXTM3U\n#EXT-X-ENDLIST"), []);
  assert.deepEqual(parseSegmentStarts("#EXTINF:abc,\nseg0.m4s"), []);
});

test("跳转就近吸附到关键帧：跳过片头落在片头结束后最近的那个", () => {
  const starts = [0, 10, 15.8, 21.6, 24.083, 29.6, 34.6, 40];
  // 片头到 33.746 结束：最近的关键帧是 34.6（再往前 29.6 要重放 4 秒片头）
  assert.equal(snapToKeyframe(starts, 33.746, 2), 34.6);
  assert.equal(snapToKeyframe(starts, 16, 2), 15.8);
});

test("吸附不能让这一跳反了方向或原地不动", () => {
  const starts = [0, 100, 110];
  // 从 103 后退 10 秒到 93：最近的是 100，仍在当前位置之前，算往回
  assert.equal(snapToKeyframe(starts, 93, 103), 100);
  // 从 101 前进到 104：最近的 100 落在当前位置之前（成了往回跳），改取前方最近的 110
  assert.equal(snapToKeyframe(starts, 104, 101), 110);
  // 从 105 后退到 102：最近的 100 在当前位置之前，正常
  assert.equal(snapToKeyframe([0, 100, 104.5], 102, 105), 100);
  // 前方一个关键帧都没有（片尾）：照原目标跳
  assert.equal(snapToKeyframe([0, 100], 120, 115), 120);
  // 没有关键帧表：照原目标
  assert.equal(snapToKeyframe([], 42, 10), 42);
});
