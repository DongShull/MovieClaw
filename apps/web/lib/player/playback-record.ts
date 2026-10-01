/**
 * 一次播放的记录（docs/design/playback-qoe.md；与 iOS App 的 `PlaybackRecord` 同一口径，
 * apps/apple/MovieClaw/Features/Player/PlaybackRecord.swift）。
 *
 * ## 为什么要有它
 * 播放体验的北极星是「无打扰播放率」：一次播放从点下到离开，有没有让用户等太久（快）、被打断（稳）、
 * 拿到打了折扣的规格或被猜错了音轨字幕（对）。网页原来只在「看满 3 秒或出过画」时报一行旧口径快照
 * （qoe.ts）：不带播放编号、跳转只有次数、失败与出画前就退出的播放一条都不报——服务端的
 * `/playback/stats/qoe` 因此只统计得到 App，两端没法放在一起比。现在网页与 App 报同一份记录。
 *
 * ## 一次播放的边界
 * 进入这一集（点播放、切集、错误页点「重试」）到离开这一集。断线重连、原位重开、降档、换画质 / 音轨
 * 都在同一次里，编号不变。
 *
 * ## 与服务端的分工
 * 编号在进入这一集时生成，随开会话请求带给服务端（服务端先建「已开始」的行、写进取流令牌，取流统计
 * 按它归集）；离开时上报完整记录（report-queue.ts）。**这里只记原始事实**：跳转分位、非自愿中断、
 * 可避免的规格损失、北极星都由服务端按规则判定（services/playback/qoe.py，规则只放一处）。
 *
 * ## 计时口径（playback-qoe.md §1.3）
 * 一律从用户动作算到画面出现，用单调时钟（performance.now）；停在用户手里的等待（软件转码同意弹窗）
 * 单独记、不算进起播。卡顿不含起播与跳转：按「用户想看、画面已出、没在跳转，播放头却不走」判。
 */

import type { PlaybackUnit } from "@/lib/api/playback";

export type RecordOrigin = "tap" | "auto_next" | "deeplink" | "retry";
export type RecordOutcome = "watched" | "exited" | "exit_before_start" | "failed" | "abnormal_exit";
/** 跳转从哪来（分组看「按键 / 拖动」哪种慢） */
export type SeekSource = "button" | "scrub" | "gesture" | "keyboard" | "remote" | "auto" | "restart";

interface SeekItem {
  seq: number;
  source: SeekSource;
  from_ms: number;
  to_ms: number;
  /** 发起时落点在前向缓冲里 */
  buffered: boolean;
  paused: boolean;
  /** 换会话式的跳转（落点在会话已转出的区间外、或会话正在重开） */
  restart: boolean;
  at_ms: number;
  /** 到画面出现的毫秒数 */
  ms: number | null;
  /** landed / superseded / failed / abandoned / timeout / pending */
  outcome: string;
}

interface SwitchItem {
  /** audio / subtitle / quality */
  kind: string;
  from: string | null;
  to: string | null;
  at_ms: number;
  ms: number | null;
}

interface InterruptionItem {
  /** rebuffer / reconnect / error / engine_failure / fallback */
  kind: string;
  at_ms: number;
  ms?: number | null;
  cause?: string | null;
  detail?: string | null;
}

interface BehaviorItem {
  /** audio_change / subtitle_change / quality_change / resume_seek / quick_exit / retry / reenter */
  kind: string;
  at_ms: number;
  since_first_frame_ms: number | null;
  from: string | null;
  to: string | null;
  /** 说明播放器猜错了（进北极星）；其余只作诊断 */
  misguess: boolean;
}

/** 规格快照（「对」的判定材料，损失由服务端按 playback-qoe.md §5.6 的规则表判） */
export interface DeliverySnapshot {
  /**
   * direct（档 0 直出原文件）/ server_remux（档 1、2，视频直通）/ server_transcode（降档或用户限画质
   * 落到的转码，属于可能可避免的损失）/ server_transcode_required（浏览器本来就解不了这个编码，
   * 转码是设备上限，不算可避免）
   */
  route: string;
  tier: number;
  user_capped: boolean;
  fallback_reason: string | null;
  video: { source_format: string | null; output_format: string | null; codec: string | null };
  audio: {
    source_codec: string | null;
    source_channels: number | null;
    delivery: string | null;
    output_codec: string | null;
  };
  subtitle: { mode: string };
  output: { audio_route: string; display_hdr: boolean };
}

interface TimelineEvent {
  at_ms: number;
  kind: string;
  text: string;
}

/** 与服务端 PlaybackMetricPayload 字段一一对应（新口径） */
export interface PlaybackRecordPayload {
  library_file_id: number | null;
  tier: number;
  degraded_from: number | null;
  engine: string;
  hw_backend: string;
  ttff_ms: number | null;
  rebuffer_ms: number;
  rebuffer_count: number;
  seek_count: number;
  dropped_frames: number | null;
  total_frames: number | null;
  watched_ms: number;
  attempt_id: string;
  outcome: RecordOutcome;
  media_item_id: number;
  season_number: number | null;
  episode_number: number | null;
  origin: RecordOrigin;
  client: "web";
  lab_scenario: string;
  route: string;
  network_class: string;
  interface: string;
  app_version: string;
  first_frame_ms: number | null;
  playing_ms: number | null;
  user_wait_ms: number;
  error_kind: string;
  error_category: string;
  error_stage: string;
  detail: Record<string, unknown>;
  log_tail: string;
}

/** 与服务端上限一致，超出的不再记 */
const MAX_ITEMS = 50;
const MAX_TIMELINE = 200;
/** 跳转落地、播放头还没重新走起来：这段时间内不判卡顿（解码起步、音频预滚） */
export const RESUME_GRACE_MS = 1500;
/** 播放头多久不走算一次卡顿（北极星的打扰线是 0.5 秒） */
export const STALL_THRESHOLD_MS = 500;
const SWITCH_TIMEOUT_MS = 15_000;
const SEEK_TIMEOUT_MS = 30_000;

/** 最近一次离开的条目（60 秒内又进同一部 = 重进，进诊断） */
let lastExit: { mediaItemId: number; at: number } | null = null;

function newAttemptId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  // 老浏览器（非安全上下文）没有 randomUUID：拼一个 v4 形状的随机串，服务端只当作不透明编号
  const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20)}`;
}

export class PlaybackRecord {
  readonly id: string;
  readonly origin: RecordOrigin;
  readonly unit: PlaybackUnit;
  /** 实验室场景名（localStorage 的 movieclaw.player.lab）；空 = 真实使用 */
  readonly lab: string;
  private readonly now: () => number;
  private readonly startedAt: number;

  // —— 会话与通路（播放器在各节点填） ——
  libraryFileId: number | null = null;
  tier = -1;
  degradedFrom: number | null = null;
  engine = "";
  hwBackend = "";
  route = "";
  /** 起播位置（毫秒）与是不是续播（续播后马上往远处跳 = 续播位置猜错了） */
  startPositionMs = 0;
  resumed = false;

  // —— 快 ——
  private firstFrameAt: number | null = null;
  private playingAt: number | null = null;
  private userWaitStartedAt: number | null = null;
  private userWaitMs = 0;
  private startupMarks: Record<string, number> = {};
  private serverTimings: Record<string, number>[] = [];
  private seeks: SeekItem[] = [];
  private openSeek: { index: number; startedAt: number } | null = null;
  private scrubStartedAt: number | null = null;
  private seekSeq = 0;
  private switches: SwitchItem[] = [];
  private openSwitch: { index: number; startedAt: number } | null = null;

  // —— 稳 ——
  private interruptions: InterruptionItem[] = [];
  private stallOpen: { startedAt: number; atMs: number; cause: string } | null = null;
  private lastPlayheadMs: number | null = null;
  private lastAdvanceAt: number | null = null;
  private awaitingResumeSince: number | null = null;
  private reconnectOpen: { startedAt: number; atMs: number; reason: string } | null = null;
  private rebufferCountValue = 0;
  private rebufferMsValue = 0;
  errorKind = "";
  errorCategory = "";
  errorStage = "";

  // —— 对 ——
  private delivery: DeliverySnapshot | null = null;
  private behaviors: BehaviorItem[] = [];
  private audioChanged = false;
  private subtitleChanged = false;
  private downlinkPeakBps: number | null = null;
  private timeline: TimelineEvent[] = [];

  constructor(options: {
    unit: PlaybackUnit;
    origin: RecordOrigin;
    /** 计时起点（单调时钟毫秒）：点播放的那一刻，没有就是进入这一集的那一刻 */
    startedAt: number;
    lab?: string;
    now?: () => number;
    id?: string;
  }) {
    this.id = options.id ?? newAttemptId();
    this.unit = options.unit;
    this.origin = options.origin;
    this.lab = options.lab ?? "";
    this.now = options.now ?? (() => performance.now());
    this.startedAt = options.startedAt;
    const previous = lastExit;
    if (previous && previous.mediaItemId === options.unit.media_item_id && this.now() - previous.at < 60_000) {
      this.behaviors.push({
        kind: "reenter",
        at_ms: 0,
        since_first_frame_ms: null,
        from: null,
        to: null,
        misguess: false,
      });
    }
    this.event("start", `开始（${options.origin}）`);
  }

  /** 距这次播放开始的毫秒数 */
  elapsedMs(at: number = this.now()): number {
    return Math.max(0, Math.round(at - this.startedAt));
  }

  private since(start: number): number {
    return Math.max(0, Math.round(this.now() - start));
  }

  private get sinceFirstFrameMs(): number | null {
    return this.firstFrameAt === null ? null : this.since(this.firstFrameAt);
  }

  get hasFirstFrame(): boolean {
    return this.firstFrameAt !== null;
  }

  get rebufferCount(): number {
    return this.rebufferCountValue;
  }

  get rebufferMs(): number {
    return this.rebufferMsValue;
  }

  event(kind: string, text: string): void {
    if (this.timeline.length >= MAX_TIMELINE) return;
    this.timeline.push({ at_ms: this.elapsedMs(), kind, text: text.slice(0, 200) });
  }

  // ---------------------------------------------------------------- 快：起播

  beginUserWait(): void {
    if (this.userWaitStartedAt === null) this.userWaitStartedAt = this.now();
    this.event("user_wait", "等用户确认");
  }

  endUserWait(): void {
    if (this.userWaitStartedAt === null) return;
    this.userWaitMs += this.since(this.userWaitStartedAt);
    this.userWaitStartedAt = null;
  }

  /** 每次新流出画都来：第一次是起播，之后结束换轨 / 重连 / 换会话式跳转的计时 */
  noteFirstFrame(): void {
    if (this.firstFrameAt === null) {
      this.firstFrameAt = this.now();
      // 起播同跳转落地：首帧上屏后播放头要过一会儿才看得出在走，给同样的起步宽限
      this.awaitingResumeSince = this.firstFrameAt;
      this.event("first_frame", "首帧出画");
    }
    this.closeSwitch();
    this.closeReconnect();
    if (this.openSeek && this.seeks[this.openSeek.index].restart) this.closeSeek("landed");
  }

  notePlaying(): void {
    if (this.playingAt === null) this.playingAt = this.now();
  }

  noteStartupMarks(marks: { name: string; ms: number }[]): void {
    for (const mark of marks) {
      if (!(mark.name in this.startupMarks)) this.startupMarks[mark.name] = mark.ms;
    }
  }

  /** 开会话响应的 Server-Timing（服务端各段耗时），分出「网络往返」与「服务端处理」 */
  noteServerTiming(timings: Record<string, number>): void {
    if (Object.keys(timings).length === 0 || this.serverTimings.length >= 10) return;
    this.serverTimings.push(timings);
  }

  // ---------------------------------------------------------------- 快：跳转

  /** 拖进度条途中（画面跟随）：连续拖动以第一次拖动为起点 */
  noteScrubActivity(): void {
    if (this.scrubStartedAt === null) this.scrubStartedAt = this.now();
  }

  beginSeek(input: {
    source: SeekSource;
    fromMs: number;
    toMs: number;
    buffered: boolean;
    paused: boolean;
    restart: boolean;
  }): void {
    if (this.openSeek) this.closeSeek("superseded");
    const start = (input.source === "scrub" ? this.scrubStartedAt : null) ?? this.now();
    this.scrubStartedAt = null;
    this.seekSeq += 1;
    const since = this.sinceFirstFrameMs;
    // 续播之后马上往远处跳：续播位置不是用户要的
    if (
      this.resumed &&
      input.source !== "auto" &&
      since !== null &&
      since <= 30_000 &&
      Math.abs(input.toMs - input.fromMs) > 120_000
    ) {
      this.addBehavior({
        kind: "resume_seek",
        at_ms: this.elapsedMs(),
        since_first_frame_ms: since,
        from: String(input.fromMs),
        to: String(input.toMs),
        misguess: true,
      });
    }
    if (this.seeks.length >= MAX_ITEMS) return;
    this.seeks.push({
      seq: this.seekSeq,
      source: input.source,
      from_ms: Math.round(input.fromMs),
      to_ms: Math.round(input.toMs),
      buffered: input.buffered,
      paused: input.paused,
      restart: input.restart,
      at_ms: this.elapsedMs(start),
      ms: null,
      outcome: "pending",
    });
    this.openSeek = { index: this.seeks.length - 1, startedAt: start };
    this.event(
      "seek",
      `跳转 #${this.seekSeq} ${Math.round(input.fromMs / 1000)} → ${Math.round(input.toMs / 1000)} 秒（${input.source}${input.buffered ? "·缓冲内" : ""}${input.restart ? "·换会话" : ""}）`,
    );
  }

  get seekInFlight(): boolean {
    return this.openSeek !== null;
  }

  /** 落点的画面到了 */
  seekPresented(): number | null {
    return this.closeSeek("landed");
  }

  closeSeek(outcome: string): number | null {
    const open = this.openSeek;
    if (!open) return null;
    this.openSeek = null;
    const spent = this.since(open.startedAt);
    const seek = this.seeks[open.index];
    seek.ms = spent;
    seek.outcome = outcome;
    if (outcome === "landed") this.awaitingResumeSince = this.now();
    this.event(`seek_${outcome}`, `跳转 #${seek.seq} ${outcome} ${spent} 毫秒`);
    return spent;
  }

  // ---------------------------------------------------------------- 快：换轨

  beginSwitch(kind: string, from: string | null, to: string | null): void {
    this.closeSwitch();
    if (this.switches.length >= MAX_ITEMS) return;
    this.switches.push({ kind, from, to, at_ms: this.elapsedMs(), ms: null });
    this.openSwitch = { index: this.switches.length - 1, startedAt: this.now() };
    this.event("switch", `${kind} ${from ?? "-"} → ${to ?? "-"}`);
  }

  /** 结束当前的换轨计时（新轨出声 / 新画面出来）；immediate = 不用重载，即刻生效 */
  closeSwitch(immediate = false): void {
    const open = this.openSwitch;
    if (!open) return;
    this.openSwitch = null;
    const spent = immediate ? 0 : this.since(open.startedAt);
    this.switches[open.index].ms = spent;
    this.event("switch_done", `${this.switches[open.index].kind} 生效 ${spent} 毫秒`);
  }

  // ---------------------------------------------------------------- 稳

  /**
   * 每 250 毫秒采一次播放头。active = 用户想看、画面已出、没在后台：这时播放头 0.5 秒不走就是卡顿。
   * 起播之前、跳转与换轨途中的等待各有各的计时，不算卡顿（CTA-2066）。
   */
  samplePlayhead(positionMs: number, active: boolean, cause: () => string): void {
    const now = this.now();
    const last = this.lastPlayheadMs;
    this.lastPlayheadMs = positionMs;
    // 保险：迟迟等不到结果的换轨作废、跳转记为超时——结束事件万一丢了，不能一直挡着卡顿检测
    if (this.openSwitch && now - this.openSwitch.startedAt > SWITCH_TIMEOUT_MS) {
      const kind = this.switches[this.openSwitch.index].kind;
      this.openSwitch = null;
      this.event("switch_timeout", `${kind} 15 秒没等到生效，作废`);
    }
    if (this.openSeek && now - this.openSeek.startedAt > SEEK_TIMEOUT_MS) this.closeSeek("timeout");
    if (!active || this.firstFrameAt === null || this.openSeek || this.openSwitch) {
      this.closeStall();
      this.lastAdvanceAt = now;
      if (!active) this.awaitingResumeSince = null;
      return;
    }
    if (last !== null && positionMs !== last) {
      this.closeStall();
      this.lastAdvanceAt = now;
      this.awaitingResumeSince = null;
      return;
    }
    if (this.awaitingResumeSince !== null) {
      if (now - this.awaitingResumeSince < RESUME_GRACE_MS) return;
      const landedAt = this.awaitingResumeSince;
      this.awaitingResumeSince = null;
      this.stallOpen = { startedAt: landedAt, atMs: this.elapsedMs(landedAt), cause: cause() };
      return;
    }
    if (this.stallOpen || this.lastAdvanceAt === null || now - this.lastAdvanceAt < STALL_THRESHOLD_MS) return;
    this.stallOpen = { startedAt: this.lastAdvanceAt, atMs: this.elapsedMs(this.lastAdvanceAt), cause: cause() };
  }

  private closeStall(): void {
    const open = this.stallOpen;
    if (!open) return;
    this.stallOpen = null;
    const spent = this.since(open.startedAt);
    this.rebufferCountValue += 1;
    this.rebufferMsValue += spent;
    this.addInterruption({ kind: "rebuffer", at_ms: open.atMs, ms: spent, cause: open.cause });
    this.event("rebuffer", `卡顿 ${spent} 毫秒（${open.cause}）`);
  }

  beginReconnect(reason: string): void {
    if (!this.reconnectOpen) this.reconnectOpen = { startedAt: this.now(), atMs: this.elapsedMs(), reason };
    this.event("reconnect", `重连：${reason}`);
  }

  get reconnecting(): boolean {
    return this.reconnectOpen !== null;
  }

  private closeReconnect(): void {
    const open = this.reconnectOpen;
    if (!open) return;
    this.reconnectOpen = null;
    this.addInterruption({ kind: "reconnect", at_ms: open.atMs, ms: this.since(open.startedAt), detail: open.reason });
  }

  /** 错误页（用户看得见的失败） */
  noteError(message: string, category: string, stage: string, kind = ""): void {
    this.errorKind = kind;
    this.errorCategory = category;
    this.errorStage = stage;
    this.addInterruption({ kind: "error", at_ms: this.elapsedMs(), cause: category, detail: message });
    this.event("error", `错误页：${message}`);
  }

  /** 播放链路报的失败（不一定让用户看到：多半被重连、原位重开接住） */
  noteEngineFailure(reason: string, cause: string): void {
    this.addInterruption({ kind: "engine_failure", at_ms: this.elapsedMs(), cause, detail: reason });
    this.event("engine_failure", `播放失败（${cause}）：${reason}`);
  }

  /** 降档（换成更低的一档重开） */
  noteFallback(reason: string): void {
    this.addInterruption({ kind: "fallback", at_ms: this.elapsedMs(), detail: reason });
    this.event("fallback", `降档：${reason}`);
  }

  private addInterruption(item: InterruptionItem): void {
    if (this.interruptions.length >= MAX_ITEMS) return;
    this.interruptions.push(item);
  }

  // ---------------------------------------------------------------- 对

  noteDelivery(snapshot: DeliverySnapshot): void {
    if (this.delivery && JSON.stringify(this.delivery) === JSON.stringify(snapshot)) return;
    if (this.delivery) {
      this.event("delivery", `规格变化：${snapshot.route} · 档 ${snapshot.tier}`);
    }
    this.delivery = snapshot;
  }

  /** 用户换音轨：首帧后 30 秒内第一次换掉自动选的轨 = 猜错了 */
  noteAudioChange(from: string | null, to: string): void {
    const since = this.sinceFirstFrameMs;
    const misguess = !this.audioChanged && (since === null || since <= 30_000);
    this.audioChanged = true;
    this.addBehavior({ kind: "audio_change", at_ms: this.elapsedMs(), since_first_frame_ms: since, from, to, misguess });
  }

  noteSubtitleChange(from: string | null, to: string | null): void {
    const since = this.sinceFirstFrameMs;
    const misguess = !this.subtitleChanged && (since === null || since <= 30_000);
    this.subtitleChanged = true;
    this.addBehavior({
      kind: "subtitle_change",
      at_ms: this.elapsedMs(),
      since_first_frame_ms: since,
      from,
      to: to ?? "off",
      misguess,
    });
  }

  noteBehavior(kind: string, from: string | null = null, to: string | null = null): void {
    this.addBehavior({ kind, at_ms: this.elapsedMs(), since_first_frame_ms: this.sinceFirstFrameMs, from, to, misguess: false });
  }

  private addBehavior(item: BehaviorItem): void {
    if (this.behaviors.length >= MAX_ITEMS) return;
    this.behaviors.push(item);
    this.event(item.kind, `${item.kind} ${item.from ?? "-"} → ${item.to ?? "-"}${item.misguess ? "（猜错）" : ""}`);
  }

  noteDownlink(bps: number | null): void {
    if (bps !== null && bps > 0) this.downlinkPeakBps = Math.max(this.downlinkPeakBps ?? 0, bps);
  }

  // ---------------------------------------------------------------- 收尾

  /** 离开时判结局：出过画且放到了片尾附近算看完；没出画按有没有落到错误页分失败 / 出画前退出 */
  outcome(input: { phaseIsError: boolean; phaseIsEnded: boolean; positionMs: number; durationMs: number | null }): RecordOutcome {
    if (input.phaseIsError) return "failed";
    if (this.firstFrameAt === null) return "exit_before_start";
    if (input.phaseIsEnded) return "watched";
    const duration = input.durationMs;
    if (duration && duration > 0 && input.positionMs >= duration - Math.max(60_000, duration / 20)) return "watched";
    return "exited";
  }

  /** 离开这次播放：记下快速退出与「最近一次离开」 */
  noteLeaving(): void {
    const since = this.sinceFirstFrameMs;
    if (since !== null && since < 10_000) this.noteBehavior("quick_exit");
    lastExit = { mediaItemId: this.unit.media_item_id, at: this.now() };
    this.event("leave", "离开");
  }

  /** 上报用的完整记录。开着的卡顿 / 重连 / 跳转都算到这一刻（不改动记录本身，可以反复取） */
  payload(input: {
    outcome: RecordOutcome;
    positionMs: number;
    durationMs: number | null;
    watchedMs: number;
    droppedFrames: number | null;
    totalFrames: number | null;
    context: Record<string, unknown>;
    logTail?: string;
  }): PlaybackRecordPayload {
    const interruptions = [...this.interruptions];
    const seeks = this.seeks.map((s) => ({ ...s }));
    let rebufferMs = this.rebufferMsValue;
    let rebufferCount = this.rebufferCountValue;
    if (this.stallOpen) {
      const spent = this.since(this.stallOpen.startedAt);
      interruptions.push({ kind: "rebuffer", at_ms: this.stallOpen.atMs, ms: spent, cause: this.stallOpen.cause });
      rebufferMs += spent;
      rebufferCount += 1;
    }
    if (this.reconnectOpen) {
      // 重连到离开都没接回来（最后落到错误页、或用户等不及退出了）
      interruptions.push({
        kind: "reconnect",
        at_ms: this.reconnectOpen.atMs,
        ms: this.since(this.reconnectOpen.startedAt),
        detail: this.reconnectOpen.reason,
      });
    }
    if (this.openSeek) {
      seeks[this.openSeek.index].ms = this.since(this.openSeek.startedAt);
      seeks[this.openSeek.index].outcome = "abandoned";
    }
    const firstFrameMs = this.firstFrameAt === null ? null : Math.max(0, this.elapsedMs(this.firstFrameAt) - this.userWaitMs);
    const playingMs = this.playingAt === null ? null : Math.max(0, this.elapsedMs(this.playingAt) - this.userWaitMs);
    const context = {
      ...input.context,
      downlink_mbps: this.downlinkPeakBps === null ? null : Math.round(this.downlinkPeakBps / 10_000) / 100,
      origin: this.origin,
      lab: this.lab,
    };
    return {
      library_file_id: this.libraryFileId,
      tier: this.tier,
      degraded_from: this.degradedFrom,
      engine: this.engine,
      hw_backend: this.hwBackend,
      ttff_ms: firstFrameMs,
      rebuffer_ms: rebufferMs,
      rebuffer_count: rebufferCount,
      seek_count: this.seeks.length,
      dropped_frames: input.droppedFrames,
      total_frames: input.totalFrames,
      watched_ms: Math.round(input.watchedMs),
      attempt_id: this.id,
      outcome: input.outcome,
      media_item_id: this.unit.media_item_id,
      season_number: this.unit.season_number ?? null,
      episode_number: this.unit.episode_number ?? null,
      origin: this.origin,
      client: "web",
      lab_scenario: this.lab,
      route: this.route,
      network_class: "unknown",
      interface: typeof input.context.interface === "string" ? input.context.interface : "",
      app_version: "",
      first_frame_ms: firstFrameMs,
      playing_ms: playingMs,
      user_wait_ms: this.userWaitMs,
      error_kind: this.errorKind,
      error_category: this.errorCategory,
      error_stage: this.errorStage,
      detail: {
        startup: {
          marks: { ...this.startupMarks },
          server_timings: this.serverTimings,
          start_position_ms: this.startPositionMs,
          resumed: this.resumed,
        },
        seeks,
        switches: this.switches.map((s) => ({ ...s })),
        interruptions,
        delivery: this.delivery,
        behaviors: [...this.behaviors],
        context,
        timeline: [...this.timeline],
        end: { position_ms: Math.round(input.positionMs), duration_ms: input.durationMs },
      },
      log_tail: input.logTail ?? "",
    };
  }
}

/** Server-Timing 头（「total;dur=729, decide;dur=330」）或 Resource Timing 的 serverTiming → 名字 → 毫秒 */
export function parseServerTiming(
  value: string | readonly { name: string; duration: number }[] | null | undefined,
): Record<string, number> {
  const timings: Record<string, number> = {};
  if (!value) return timings;
  if (typeof value !== "string") {
    for (const entry of value) {
      if (entry.name && Number.isFinite(entry.duration)) timings[entry.name] = Math.round(entry.duration);
    }
    return timings;
  }
  for (const part of value.split(",")) {
    const [name, ...params] = part.trim().split(";");
    const dur = params.map((p) => p.trim()).find((p) => p.startsWith("dur="));
    const ms = dur ? Number(dur.slice(4)) : NaN;
    if (name && Number.isFinite(ms)) timings[name.trim()] = Math.round(ms);
  }
  return timings;
}

/**
 * 实验室场景名：localStorage 的 `movieclaw.player.lab`（实验台在页面加载前写进去）。记录带上它，
 * 服务端的北极星统计按它排除测试播放（与 iOS 的 -mcLab 启动参数同一用途）。真实使用为空。
 */
export function readLabScenario(): string {
  try {
    return (window.localStorage.getItem("movieclaw.player.lab") ?? "").slice(0, 64);
  } catch {
    return "";
  }
}
