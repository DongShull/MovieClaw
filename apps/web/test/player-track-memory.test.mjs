import assert from "node:assert/strict";
import test from "node:test";

import { reportedTracks } from "../lib/player/track-memory.ts";

const base = { requestedAudio: null, subtitleTouched: false, selectedSubtitle: null, fileId: 7 };

test("没动过音轨、字幕：两条都不报，只带正在放的文件", () => {
  // 服务端的默认挑选、自动开着的字幕都不是用户的选择，报了就会被记成记忆
  assert.deepEqual(reportedTracks({ ...base, selectedSubtitle: "external:zh.srt" }), { file_id: 7 });
});

test("点选过的音轨照报", () => {
  assert.deepEqual(reportedTracks({ ...base, requestedAudio: "embedded:2" }), {
    audio_track: "embedded:2",
    file_id: 7,
  });
});

test("动过字幕：选中的轨照报，关掉报 off", () => {
  assert.deepEqual(
    reportedTracks({ ...base, subtitleTouched: true, selectedSubtitle: "embedded:1" }),
    { subtitle_track: "embedded:1", file_id: 7 },
  );
  assert.deepEqual(reportedTracks({ ...base, subtitleTouched: true }), {
    subtitle_track: "off",
    file_id: 7,
  });
});

test("不知道正在放哪个文件时不带 file_id", () => {
  assert.deepEqual(reportedTracks({ ...base, fileId: undefined }), {});
  assert.deepEqual(reportedTracks({ ...base, fileId: null }), {});
});
