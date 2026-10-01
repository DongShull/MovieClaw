/**
 * 播放记录的上报队列（docs/design/playback-qoe.md §2；与 iOS App 的 `PlaybackReportQueue` 同一做法）。
 *
 * ## 为什么要落本地
 * 最该上报的恰恰是最容易丢的那几次播放：出错退出时网络可能正断着，标签页崩溃、浏览器被杀时根本
 * 来不及发。所以发不出去的记录先存进本浏览器的 localStorage，下一次打开播放器时补发；服务端按
 * 播放编号合并，重复上报只留最后一份，补发不会重复计数。
 *
 * ## 异常退出
 * 播放期间每 10 秒刷新一份「正在播放」标记（内容就是这一刻的完整记录，结局写成「异常退出」）。
 * 正常离开会删掉它；下次打开播放器时还看得到、且 30 秒没刷新过（别的标签页正播着的会一直刷新），
 * 说明那次播放没能收尾（标签页崩溃、浏览器被杀、系统休眠），把它转进队列补报。
 *
 * 遥测只发给用户自己的服务器（硬边界 3）；影片分享的访客不上报（调用方按作用域跳过）。
 */

import type { PlaybackRecordPayload } from "./playback-record";

const QUEUE_KEY = "movieclaw.player.report-queue";
const ACTIVE_PREFIX = "movieclaw.player.active.";
/** 队列最多留几条、几天（一直发不出去的，例如服务器已经不用了） */
export const MAX_QUEUE = 20;
export const MAX_AGE_MS = 7 * 24 * 3600 * 1000;
/** 「正在播放」标记多久没刷新算没能收尾（播放中每 10 秒刷新一次） */
export const ACTIVE_STALE_MS = 30_000;

export interface StoredReport {
  payload: PlaybackRecordPayload;
  savedAt: number;
}

/** 可替换的存储（测试注入内存实现）；拿不到 localStorage 时一切操作静默跳过 */
export interface ReportStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  keys(): string[];
}

export function browserReportStorage(): ReportStorage | null {
  try {
    const storage = window.localStorage;
    return {
      getItem: (key) => storage.getItem(key),
      setItem: (key, value) => storage.setItem(key, value),
      removeItem: (key) => storage.removeItem(key),
      keys: () => Array.from({ length: storage.length }, (_, i) => storage.key(i) ?? "").filter(Boolean),
    };
  } catch {
    return null;
  }
}

function readQueue(storage: ReportStorage, now: number): StoredReport[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(QUEUE_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is StoredReport =>
        !!item &&
        typeof item === "object" &&
        typeof (item as StoredReport).savedAt === "number" &&
        now - (item as StoredReport).savedAt < MAX_AGE_MS &&
        typeof (item as StoredReport).payload?.attempt_id === "string",
    );
  } catch {
    return [];
  }
}

function writeQueue(storage: ReportStorage, queue: StoredReport[]): void {
  try {
    if (queue.length === 0) storage.removeItem(QUEUE_KEY);
    else storage.setItem(QUEUE_KEY, JSON.stringify(queue.slice(-MAX_QUEUE)));
  } catch {
    // 存储写满 / 隐私模式：这一份就丢了，播放不受影响
  }
}

/** 放进队列（同一编号只留最新一份） */
export function enqueueReport(storage: ReportStorage, payload: PlaybackRecordPayload, now: number): void {
  const queue = readQueue(storage, now).filter((item) => item.payload.attempt_id !== payload.attempt_id);
  queue.push({ payload, savedAt: now });
  writeQueue(storage, queue);
}

/** 刷新「正在播放」标记（结局写成异常退出：真走到补报那一步，说明它没能收尾） */
export function markActive(storage: ReportStorage, payload: PlaybackRecordPayload, now: number): void {
  try {
    storage.setItem(
      ACTIVE_PREFIX + payload.attempt_id,
      JSON.stringify({ payload: { ...payload, outcome: "abnormal_exit" }, savedAt: now }),
    );
  } catch {
    // 同上
  }
}

export function clearActive(storage: ReportStorage, attemptId: string): void {
  try {
    storage.removeItem(ACTIVE_PREFIX + attemptId);
  } catch {
    // 同上
  }
}

/** 没能收尾的播放（标记还在、且已经 30 秒没刷新）转进队列；返回转了几条 */
export function recoverAbnormalExits(storage: ReportStorage, now: number): number {
  let recovered = 0;
  for (const key of storage.keys()) {
    if (!key.startsWith(ACTIVE_PREFIX)) continue;
    let item: StoredReport | null = null;
    try {
      item = JSON.parse(storage.getItem(key) ?? "null") as StoredReport | null;
    } catch {
      item = null;
    }
    if (!item || typeof item.savedAt !== "number" || typeof item.payload?.attempt_id !== "string") {
      storage.removeItem(key);
      continue;
    }
    if (now - item.savedAt < ACTIVE_STALE_MS) continue; // 别的标签页还在播
    storage.removeItem(key);
    if (now - item.savedAt < MAX_AGE_MS) {
      enqueueReport(storage, item.payload, now);
      recovered += 1;
    }
  }
  return recovered;
}

/**
 * 把队列里的记录逐条发出去：发成功的删掉，遇到第一次失败就停（多半是网络还没好，后面的也发不出去）。
 * 返回发成功的条数。
 */
export async function flushReports(
  storage: ReportStorage,
  send: (payload: PlaybackRecordPayload) => Promise<void>,
  now: number,
): Promise<number> {
  const pending = readQueue(storage, now);
  const delivered = new Map<string, number>();
  for (const item of pending) {
    try {
      await send(item.payload);
    } catch {
      break;
    }
    delivered.set(item.payload.attempt_id, item.savedAt);
  }
  if (delivered.size > 0) {
    // 发送是异步的，期间可能又有记录进队（含同一编号的更新版）：按「编号 + 存入时刻」只删发出去的那几份
    const rest = readQueue(storage, now).filter((item) => {
      const sentAt = delivered.get(item.payload.attempt_id);
      return sentAt === undefined || item.savedAt > sentAt;
    });
    writeQueue(storage, rest);
  }
  return delivered.size;
}
