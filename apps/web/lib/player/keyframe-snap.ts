/**
 * 原生 HLS 跳转吸附到关键帧（iPhone / iPad 的 HEVC，见 playback-mode.ts）。
 *
 * 为什么要吸附：Safari 的原生 HLS（AVPlayer）跳到非关键帧时，要从前一个关键帧把中间的画面
 * 全部解一遍才追得上落点。4K HDR 的片子关键帧最长 10 秒一个，4K60 最多要白解 600 帧——花的是
 * 手机的解码功耗和落地前的等待。直接落在关键帧上，解出来的第一帧就是落点。
 *
 * 关键帧从哪来：直通档每个关键帧切一段（服务端 hls_vod.compute_keyframe_plan），媒体列表里
 * 每一段的起点就是一个关键帧；转码档按 4 秒栅格强插关键帧，同样是每段起点。所以读一遍
 * 播放列表、把 EXTINF 累加起来就是关键帧表，与播放器（AVPlayer）自己的时间轴同一个口径。
 */

/** 媒体播放列表 → 每个分片的起点（秒，升序，首元素 0）。解析不出任何分片时为空表。 */
export function parseSegmentStarts(playlist: string): number[] {
  const starts: number[] = [];
  let at = 0;
  for (const line of playlist.split("\n")) {
    if (!line.startsWith("#EXTINF:")) continue;
    const duration = Number.parseFloat(line.slice("#EXTINF:".length));
    if (!Number.isFinite(duration) || duration < 0) return [];
    starts.push(at);
    at += duration;
  }
  return starts;
}

/**
 * 把跳转目标吸附到最近的分片起点（关键帧）。
 *
 * 就近吸附，但**不能让这一跳反了方向或原地不动**：「后退 10 秒」吸到当前所在分片的起点只退了
 * 两三秒还算往回，吸到当前位置之后就成了往前跳；往前同理。最近的那个落在反方向时，改取
 * 这一侧离当前位置最近的那个分片起点；这一侧一个都没有（片头 / 片尾）就照原目标跳。
 */
export function snapToKeyframe(starts: readonly number[], target: number, current: number): number {
  if (starts.length === 0) return target;
  let nearest = starts[0];
  for (const start of starts) {
    if (Math.abs(start - target) < Math.abs(nearest - target)) nearest = start;
    else if (Math.abs(start - target) === Math.abs(nearest - target)) nearest = Math.max(nearest, start);
  }
  if (target > current && nearest <= current) {
    const ahead = starts.find((start) => start > current);
    return ahead ?? target;
  }
  if (target < current && nearest >= current) {
    let behind: number | null = null;
    for (const start of starts) if (start < current) behind = start;
    return behind ?? target;
  }
  return nearest;
}
