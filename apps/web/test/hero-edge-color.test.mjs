import assert from "node:assert/strict";
import test from "node:test";

import { pageColorFromEdge, visibleRect } from "../lib/hero-edge-color.ts";

test("露出区域按 object-cover 居中裁切计算", () => {
  // 横版剧照放进竖向画框：左右被裁
  assert.deepEqual(visibleRect(1280, 720, 0.8), { x: 352, y: 0, width: 576, height: 720 });
  // 竖版海报放进横向画框：上下被裁
  assert.deepEqual(visibleRect(500, 750, 2), { x: 0, y: 250, width: 500, height: 250 });
});

test("底边色换算页面底色：亮度封顶 0.34、暗色原样、灰色不带色相", () => {
  // 纯白 → 亮度压到 0.34 的灰
  assert.equal(pageColorFromEdge(255, 255, 255), "rgb(87 87 87)");
  // 本来就暗：保持
  assert.equal(pageColorFromEdge(20, 20, 20), "rgb(20 20 20)");
  // 亮红：色相不变、亮度封顶
  assert.equal(pageColorFromEdge(255, 0, 0), "rgb(87 0 0)");
});
