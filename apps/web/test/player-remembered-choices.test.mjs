import assert from "node:assert/strict";
import test from "node:test";

import { rememberedChoicesNotice, shortTrackLabel } from "../lib/player/remembered-choices.ts";

test("只提不是默认的项；都是默认就不打扰", () => {
  assert.equal(rememberedChoicesNotice({ quality: null, audio: null, subtitle: null }), null);
  assert.equal(
    rememberedChoicesNotice({ quality: 720, audio: "日语", subtitle: "关闭" }),
    "已沿用上次的选择：画质 720p，音轨 日语，字幕 关闭",
  );
  assert.equal(rememberedChoicesNotice({ quality: null, audio: "日语", subtitle: null }), "已沿用上次的选择：音轨 日语");
});

test("标签只留语言；同语言有几条时留全称才分得清", () => {
  const labels = ["日语 · AC3 · 5.1", "国语 · AAC · 立体声", "国语 · DTS · 5.1"];
  assert.equal(shortTrackLabel("日语 · AC3 · 5.1", labels), "日语");
  assert.equal(shortTrackLabel("国语 · DTS · 5.1", labels), "国语 · DTS · 5.1");
});
