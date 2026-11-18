import { http } from '@kit.NetworkKit';

/**
 * 网络测速（对齐 iOS `NetBench.swift` 的思路）：
 *
 * 1. RTT：对 `GET /api/v1/health` 连测 3 次（端点小、不碰业务），取最小值——
 *    最小值最接近纯往返，不受偶发调度毛刺影响。
 * 2. 吞吐：对当前播放流地址（`session.stream_url`，直出/转码都是它）用
 *    `requestInStream` 限时拉流，`dataReceiveProgress` 累计字节数，
 *    按采样窗口折算 Mbit/s（窗口前后字节差 / 窗口时长，去掉建连/起流的预热）。
 *
 * 纯计算函数单独拆出（`mbpsOf`），entry/src/test/PlayerDiagnostics.test.ets 有单测。
 */

/** 一次测速的完整读数；错误不抛出，落在 `error` 里给界面标红 */
export class NetBenchResult {
  /** RTT（毫秒）；-1 = 测不通 */
  rttMs: number = -1;
  /** 采样窗口内收到的字节数；0 = 没收到数据 */
  bytes: number = 0;
  /** 采样窗口时长（秒，含小数） */
  seconds: number = 0;
  /** 失败原因（可读文案，界面标红展示） */
  error: string = '';

  /** 界面逐行展示的读数 */
  lines(): string[] {
    const out: string[] = [];
    out.push(this.rttMs >= 0 ? `往返延迟：${this.rttMs} ms` : '往返延迟：测不通');
    if (this.bytes > 0 && this.seconds > 0) {
      out.push(`下载：${this.seconds.toFixed(1)} 秒收到 ${Math.round(this.bytes / 1048576)} MB · ${NetBench.mbpsOf(this.bytes, this.seconds * 1000)} Mbit/s`);
    } else if (this.error.length > 0) {
      out.push(this.error);
    }
    return out;
  }

  /** 吞吐行是否该标红（没量到任何数据） */
  get failed(): boolean {
    return this.bytes <= 0;
  }
}

export class NetBench {
  /** 纯计算：bytes 字节耗时 ms 毫秒 → Mbit/s（保留 1 位小数） */
  static mbpsOf(bytes: number, ms: number): number {
    if (bytes <= 0 || ms <= 0) {
      return 0;
    }
    return Math.round(bytes * 8 / (ms / 1000) / 1_000_000 * 10) / 10;
  }

  static sleep(ms: number): Promise<void> {
    return new Promise<void>((resolve: () => void) => {
      setTimeout(resolve, ms);
    });
  }

  /** `GET /api/v1/health` 连测 3 次取最小 RTT（毫秒）；全失败返回 -1 */
  static async rttOf(origin: string, token: string): Promise<number> {
    let best: number = -1;
    for (let i = 0; i < 3; i++) {
      const client = http.createHttp();
      try {
        const header: Record<string, string> = { 'Accept': 'application/json' };
        if (token.length > 0) {
          header['Authorization'] = `Bearer ${token}`;
        }
        const started: number = Date.now();
        await client.request(`${origin}/api/v1/health`, {
          method: http.RequestMethod.GET,
          header: header,
          expectDataType: http.HttpDataType.STRING,
          connectTimeout: 5000,
          readTimeout: 5000
        });
        const cost: number = Date.now() - started;
        if (best < 0 || cost < best) {
          best = cost;
        }
      } catch (error) {
        // 单次失败不算整体失败，继续下一轮
      } finally {
        client.destroy();
      }
    }
    return best;
  }

  /**
   * 对流地址限时拉流测吞吐：请求建好后等 windowMs 毫秒，用窗口前后累计字节差
   * 折算 Mbit/s。流地址为空（本机副本）直接给提示，不发请求。
   */
  static async throughputOf(url: string, token: string, windowMs: number): Promise<NetBenchResult> {
    const result: NetBenchResult = new NetBenchResult();
    if (url.length === 0) {
      result.error = '本机副本播放不经过服务器，无需测速';
      return result;
    }
    const client = http.createHttp();
    let totalBytes: number = 0;
    try {
      const header: Record<string, string> = { 'Range': 'bytes=0-' };
      if (token.length > 0) {
        header['Authorization'] = `Bearer ${token}`;
      }
      client.on('dataReceiveProgress', (data: http.DataReceiveProgressInfo) => {
        totalBytes = data.receiveSize;
      });
      await client.requestInStream(url, {
        method: http.RequestMethod.GET,
        header: header,
        connectTimeout: 5000,
        readTimeout: windowMs + 2000
      });
      // 响应头到了才开始计时：预热段（建连、起流）不计入吞吐
      const from: number = totalBytes;
      const started: number = Date.now();
      await NetBench.sleep(windowMs);
      result.bytes = Math.max(0, totalBytes - from);
      result.seconds = Math.max(0.1, (Date.now() - started) / 1000);
      if (result.bytes <= 0) {
        result.error = '服务器没有返回数据，测不出吞吐';
      }
    } catch (error) {
      result.error = '下载测速失败（网络或服务器不可达）';
    } finally {
      client.off('dataReceiveProgress');
      client.destroy();
    }
    return result;
  }

  /** 完整测速：吞吐 + RTT 合进一份读数（内部全捕获，不 reject） */
  static async run(origin: string, token: string, streamUrl: string, windowMs: number): Promise<NetBenchResult> {
    const result: NetBenchResult = await NetBench.throughputOf(streamUrl, token, windowMs);
    result.rttMs = origin.length > 0 ? await NetBench.rttOf(origin, token) : -1;
    return result;
  }
}
