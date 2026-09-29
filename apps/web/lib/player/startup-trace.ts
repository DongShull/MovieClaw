/**
 * 网页起播分段计时（与 iOS App 的 PlaybackStartupTrace 同一口径，docs/design/playback-startup.md）。
 *
 * QoE 的首帧只有一个总数，用户说「这部片点了半天才出画」时看不出慢在哪：跳路由、能力探测、
 * 开会话（服务端决策、拉起 ffmpeg）、挂流、取首片、首帧解码……这里按发生顺序记下每个点距起点
 * 的毫秒数，首帧上屏且开始播放后上报一次（client-log 的 startup 事件，服务端记成 INFO 一行
 * 「起播分段」）。起点优先取用户点播放的那一刻（`markPlayIntent`），直开播放页时取进入这一集的时刻。
 *
 * 只记每一集的第一次出画：之后的 seek、换轨、降档不在这里算（它们有各自的 QoE 口径）。
 */

export interface StartupMark {
  name: string;
  ms: number;
}

export class StartupTrace {
  private readonly origin: number;
  private readonly marks: StartupMark[] = [];
  private reported = false;

  constructor(origin: number) {
    this.origin = origin;
  }

  /** 记一个点；同名点只记第一次（事件可能重复到达），上报之后不再记 */
  mark(name: string, at: number = performance.now()): void {
    if (this.reported || this.marks.some((mark) => mark.name === name)) return;
    this.marks.push({ name, ms: Math.round(at - this.origin) });
  }

  has(name: string): boolean {
    return this.marks.some((mark) => mark.name === name);
  }

  /**
   * 该上报了就交出按时间排好的计时点（每集只交一次）：首帧上屏且已开始播放，或 `force`
   * （开始播放后迟迟等不到首帧信号——少数浏览器没有 requestVideoFrameCallback）。
   */
  finish(force = false): StartupMark[] | null {
    if (this.reported || !this.has("播放") || !(force || this.has("首帧"))) return null;
    this.reported = true;
    return [...this.marks].sort((a, b) => a.ms - b.ms);
  }
}
