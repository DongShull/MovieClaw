import assert from "node:assert/strict";
import test from "node:test";

import {
  DECODE_STALL_MIN_BUFFER_S,
  DIRECT_DEAD_S,
  MAX_NUDGES,
  NUDGE_AT_S,
  SERVER_DEAD_S,
  STALL_TIMEOUT_S,
  bufferedAhead,
  classifyStall,
  shouldNudge,
  stallReason,
} from "../lib/player/stall.ts";

// 与 iOS 的 StallWatch 同一套规则（MovieClawTests/PlaybackWatchdogsTests.swift）：
// 缓冲见底时只看字节还在不在进来——在进来就是线路慢，一直等；连续十几秒一个字节都没有才算断线。
const base = {
  paused: false,
  ended: false,
  seeking: false,
  advanced: false,
  bufferedAhead: 10,
  stalledFor: 0,
  silentFor: 0,
};

test("正在前进就不是停顿", () => {
  assert.equal(classifyStall({ ...base, advanced: true, stalledFor: 99, silentFor: 99 }), "ok");
});

for (const flag of ["paused", "ended", "seeking"]) {
  test(`${flag} 期间不算停顿——用户自己按的暂停不该被当成故障`, () => {
    assert.equal(classifyStall({ ...base, [flag]: true, stalledFor: 99, silentFor: 99 }), "ok");
  });
}

test("缓冲里有数据却放不动 = 解码器卡死", () => {
  assert.equal(classifyStall({ ...base, bufferedAhead: 10, stalledFor: STALL_TIMEOUT_S }), "decode-stalled");
  assert.equal(classifyStall({ ...base, bufferedAhead: 10, stalledFor: STALL_TIMEOUT_S - 1 }), "ok");
});

test("线路慢：缓冲见底但字节一直在进来，等多久都不判失败（不再自动转码降画质）", () => {
  // 2026-09-28 iOS 拍板「网络问题不是降级信号」：原来这里等满 45 秒（直出 15 秒）就判缺粮，
  // 网页随即自动把直通档改成转码
  assert.equal(classifyStall({ ...base, bufferedAhead: 0, stalledFor: 600, silentFor: 0 }), "ok");
  assert.equal(classifyStall({ ...base, bufferedAhead: 1.5, stalledFor: 600, silentFor: 0 }), "ok");
});

test("连接断了：缓冲见底且连续没有字节，服务端流 45 秒、直出 15 秒", () => {
  const empty = { ...base, bufferedAhead: 0, stalledFor: 999 };
  assert.equal(classifyStall({ ...empty, silentFor: SERVER_DEAD_S - 1 }), "ok");
  assert.equal(classifyStall({ ...empty, silentFor: SERVER_DEAD_S }), "dead");
  assert.equal(classifyStall({ ...empty, silentFor: DIRECT_DEAD_S, deadLimitS: DIRECT_DEAD_S }), "dead");
  assert.equal(classifyStall({ ...empty, silentFor: DIRECT_DEAD_S - 1, deadLimitS: DIRECT_DEAD_S }), "ok");
  assert.ok(DIRECT_DEAD_S < SERVER_DEAD_S);
  assert.ok(DIRECT_DEAD_S > STALL_TIMEOUT_S);
});

test("前方剩一两秒卡住是在等数据，不是解码卡死：按断线规则判，不按 8 秒判", () => {
  // 转码会话里 buffered 尾 = 已转出的全部，播放头贴着尾巴跑时前方常剩 0.5~2 秒（真机踩中：
  // 「选个 PGS 字幕先给我降了一档」）
  assert.equal(classifyStall({ ...base, bufferedAhead: 1.5, stalledFor: STALL_TIMEOUT_S }), "ok");
  assert.equal(
    classifyStall({ ...base, bufferedAhead: DECODE_STALL_MIN_BUFFER_S, stalledFor: STALL_TIMEOUT_S }),
    "decode-stalled",
  );
});

test("两种原因给的是不同的中文说法，断线按直出 / 服务端流说清是谁没数据", () => {
  assert.match(stallReason("decode-stalled"), /吃不下/);
  assert.match(stallReason("dead", DIRECT_DEAD_S), /连续 15 秒没有收到数据/);
  assert.match(stallReason("dead"), /服务端/);
});

function fakeVideo(currentTime, ranges) {
  return {
    currentTime,
    buffered: {
      length: ranges.length,
      start: (i) => ranges[i][0],
      end: (i) => ranges[i][1],
    },
  };
}

test("前方缓冲取的是当前所在的那段", () => {
  assert.equal(bufferedAhead(fakeVideo(5, [[0, 12]])), 7);
});

test("落在缓冲空洞里记 0", () => {
  assert.equal(bufferedAhead(fakeVideo(20, [[0, 12], [30, 40]])), 0);
});

test("没有任何缓冲记 0", () => {
  assert.equal(bufferedAhead(fakeVideo(0, [])), 0);
});

// ---------------------------------------------------------------------------
// 推一把（nudge）：iOS AVPlayer「有数据却楞住」的 wedge 先踢再判死
// ---------------------------------------------------------------------------

const nudgeBase = {
  stalledFor: NUDGE_AT_S,
  nudges: 0,
  bufferedAhead: 8,
  readyState: 3,
  everAdvanced: true,
};

test("播起来过之后有数据卡满 3 秒且还有推动额度 → 推一把", () => {
  assert.equal(shouldNudge({ ...nudgeBase }), true);
});

test("卡的时间不足推动起点 → 不推", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: NUDGE_AT_S - 1 }), false);
});

test("推满次数后不再推——让 decode-stalled 判定接手降档", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, nudges: MAX_NUDGES }), false);
});

test("前方缓冲不足（追上编码器）不推——那是缺粮不是 wedge", () => {
  assert.equal(
    shouldNudge({ ...nudgeBase, stalledFor: 99, bufferedAhead: DECODE_STALL_MIN_BUFFER_S - 1 }),
    false,
  );
});

test("从未真正播起来过绝不推——起播预滚被推动打断是真机踩过的回归", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, everAdvanced: false }), false);
});

test("readyState 不足 HAVE_FUTURE_DATA（还在预滚）绝不推", () => {
  assert.equal(shouldNudge({ ...nudgeBase, stalledFor: 99, readyState: 2 }), false);
});
