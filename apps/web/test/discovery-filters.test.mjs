import assert from "node:assert/strict";
import test from "node:test";

import {
  clearDiscoveryDimension,
  discoveryDimensionSummary,
  discoveryDimensionValue,
  discoveryFilterButtonTitle,
  discoveryFilterCount,
  discoveryFiltersQuery,
  EMPTY_DISCOVERY_FILTERS,
  parseDiscoveryFilters,
  withDiscoveryDimension,
} from "../lib/discovery-filters.ts";

test("六维筛选可从分享 URL 安全恢复并重新序列化", () => {
  const filters = parseDiscoveryFilters({
    genres: "878,28,878,bad",
    country: "jp",
    year: "2025",
    rating: "7",
    runtime: "90",
    sort: "rating",
  });

  assert.deepEqual(filters, {
    genreIds: [878, 28],
    originCountry: "JP",
    year: 2025,
    ratingGte: 7,
    runtimeLte: 90,
    sort: "rating",
  });
  assert.equal(discoveryFilterCount(filters), 6);
  assert.equal(
    discoveryFiltersQuery(filters),
    "genres=878%2C28&country=JP&year=2025&rating=7&runtime=90&sort=rating",
  );
});

test("非法筛选值被忽略并回退热门排序", () => {
  assert.deepEqual(
    parseDiscoveryFilters({
      genres: "-1,0,nope",
      country: "Japan",
      year: "2200",
      rating: "11",
      runtime: "0",
      sort: "unknown",
    }),
    { genreIds: [], sort: "popular" },
  );
});

test("筛选键只写类型：全部 / 单个类型名 / 「某某等 N 个」", () => {
  const names = new Map([
    [28, "动作"],
    [35, "喜剧"],
  ]);
  assert.equal(discoveryFilterButtonTitle(EMPTY_DISCOVERY_FILTERS, names), "全部");
  assert.equal(discoveryFilterButtonTitle({ ...EMPTY_DISCOVERY_FILTERS, genreIds: [28] }, names), "动作");
  assert.equal(
    discoveryFilterButtonTitle({ ...EMPTY_DISCOVERY_FILTERS, genreIds: [28, 35] }, names),
    "动作等 2 个",
  );
  // 类型名还没到：给计数
  assert.equal(discoveryFilterButtonTitle({ ...EMPTY_DISCOVERY_FILTERS, genreIds: [28, 35] }), "2 个类型");
});

test("结果页胶囊摘要：未启用为 undefined，排序默认档不算启用", () => {
  const names = new Map([
    [28, "动作"],
    [35, "喜剧"],
    [18, "剧情"],
  ]);
  const filters = parseDiscoveryFilters({ genres: "28,35", country: "JP", year: "2024", rating: "8" });
  assert.equal(discoveryDimensionSummary("genres", filters, names), "动作、喜剧");
  assert.equal(
    discoveryDimensionSummary("genres", { ...filters, genreIds: [28, 35, 18] }, names),
    "动作等 3 个",
  );
  assert.equal(discoveryDimensionSummary("country", filters), "日本");
  assert.equal(discoveryDimensionSummary("year", filters), "2024 年");
  assert.equal(discoveryDimensionSummary("rating", filters), "8 分以上");
  assert.equal(discoveryDimensionSummary("runtime", filters), undefined);
  assert.equal(discoveryDimensionSummary("sort", filters), undefined);
  assert.equal(discoveryDimensionSummary("sort", { ...filters, sort: "newest" }), "最新优先");
});

test("单选维度的设值与移除互为逆操作", () => {
  const withYear = withDiscoveryDimension(EMPTY_DISCOVERY_FILTERS, "year", "2020");
  assert.deepEqual(withYear, { genreIds: [], sort: "popular", year: 2020 });
  assert.equal(discoveryDimensionValue(withYear, "year"), "2020");
  assert.deepEqual(withDiscoveryDimension(withYear, "year", ""), EMPTY_DISCOVERY_FILTERS);
  assert.deepEqual(clearDiscoveryDimension(withYear, "year"), EMPTY_DISCOVERY_FILTERS);
  const sorted = withDiscoveryDimension(EMPTY_DISCOVERY_FILTERS, "sort", "rating");
  assert.equal(discoveryFilterCount(sorted), 1);
  assert.equal(clearDiscoveryDimension(sorted, "sort").sort, "popular");
});
