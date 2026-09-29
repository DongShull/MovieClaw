"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import type { Route } from "next";

import { ActivityGroup, GroupLinkRow } from "@/components/activity-group";
import { useConfirm, useToast } from "@/components/feedback";
import { ArrowDownIcon, ClockIcon } from "@/components/icons";
import type { DownloadTask } from "@/lib/api/downloaders";
import {
  type BoostPool,
  type BoostPoolSite,
  type BoostPoolTask,
  cleanupBoostPool,
  getBoostPool,
} from "@/lib/api/sites";
import {
  boostSites,
  boostSummary,
  boostTotals,
  sortBoostTasks,
  type BoostMode,
} from "@/lib/activity-overview";
import { boostCleanupSummary, formatCleanupDeadline } from "@/lib/boost-cleanup";
import { useDownloadTasks } from "@/lib/download-tasks";
import { formatBytes } from "@/lib/format";
import { usePageChrome } from "@/lib/page-chrome";
import { useTaskActivity } from "@/lib/task-activity";

/**
 * 刷流做种：在池概况、清理流程、总览上的汇总行与银玻璃手机的「刷流做种」二级页。
 *
 * 任务视角（task-center-view.tsx 的 BoostTaskSection，桌面与 Netflix 主题）和手机二级页
 * 共用在池概况与清理流程（useBoostPool / useBoostCleanup），版式各走各的：二级页对齐
 * 原生 App 的 ActivityBoostPage（ActivityPages.swift）与 BoostTaskRow（TaskCards.swift）。
 */

/**
 * 实时速度的统一配色：上传走 --ok 绿（做种的"战果"），下载走 --info 蓝（"正在进行"），
 * 数值加粗从灰色标签里跳出来。所有来自下载器（qB/Tr）的任务——刷流汇总、刷流单行、
 * 普通任务的实时行——都走这一个组件，保证任务中心里 ↑/↓ 的颜色语义处处一致。
 * glow 只给刷流汇总头部用（折叠时也要一眼可见）；speed 为 0/空时给破折号或灰字占位，
 * 由调用方决定是否渲染占位（固定列需要占位防塌陷，自由流式行则直接不渲染）。
 */
export function SpeedStat({
  direction,
  bytesPerSecond,
  glow = false,
  placeholder,
  className,
}: {
  direction: "up" | "down";
  bytesPerSecond: number | null | undefined;
  glow?: boolean;
  /** 速度为空/0 时的占位文案；不传则渲染 null */
  placeholder?: string;
  className?: string;
}) {
  const active = bytesPerSecond != null && bytesPerSecond > 0;
  if (!active) {
    return placeholder != null ? (
      <span className={`tnum text-white/20 ${className ?? ""}`}>{placeholder}</span>
    ) : null;
  }
  const tone =
    direction === "up"
      ? `text-[var(--ok)] ${glow ? "drop-shadow-[0_0_6px_rgba(74,222,128,0.45)]" : ""}`
      : `text-[var(--info)] ${glow ? "drop-shadow-[0_0_6px_rgba(127,176,255,0.45)]" : ""}`;
  return (
    <span className={`tnum font-semibold ${tone} ${className ?? ""}`}>
      {direction === "up" ? "↑" : "↓"} {formatBytes(bytesPerSecond)}/s
    </span>
  );
}

/**
 * 刷流在池概况（站点开关 / 暂停、保留期、待清理）。只在有刷流种子时取；种子数变化
 * （汰换、清理、新抢入）时重取。取失败保留 null——各处据此一律按「运行中」讲，不替用户
 * 下「已关闭」的结论。
 */
export function useBoostPool(tasks: DownloadTask[]) {
  const [pool, setPool] = useState<BoostPool | null>(null);
  const count = tasks.length;
  useEffect(() => {
    if (count === 0) return;
    let cancelled = false;
    getBoostPool()
      .then((next) => !cancelled && setPool(next))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [count]);
  return { pool, setPool };
}

/**
 * 清理残留刷流种子（docs/design/site-protection-ratio-boost.md §2.9）：关闭刷流不会删种，
 * 残留种子一直满速做种、占着磁盘。先取最新概况再确认（刚关掉刷流 / 保留期刚过，旧数据
 * 会讲错后果）：删多少、还开着刷流的站点会一并关闭、保留期内的默认到期后自动删，勾选才
 * 立即全删（可能被记 H&R）。
 */
export function useBoostCleanup({
  onPool,
  onChanged,
}: {
  onPool: (pool: BoostPool) => void;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [cleaning, setCleaning] = useState(false);

  const cleanup = useCallback(async () => {
    if (cleaning) return;
    let latest: BoostPool;
    try {
      latest = await getBoostPool();
      onPool(latest);
    } catch (e) {
      toast.error((e as Error).message);
      return;
    }
    const sum = (pick: (site: BoostPoolSite) => number) =>
      latest.sites.reduce((total, site) => total + pick(site), 0);
    const count = sum((site) => site.task_count);
    const protectedCount = sum((site) => site.protected_count);
    const enabledNames = latest.sites.filter((site) => site.boost_enabled).map((site) => site.site_name);
    const until = formatCleanupDeadline(
      latest.sites.map((site) => site.protected_until).filter(Boolean).sort().at(-1),
    );
    const bullets = [
      `从下载器删除 ${count} 个刷流种子及其数据文件（${formatBytes(sum((site) => site.size_bytes))}），无法恢复`,
      ...(enabledNames.length > 0
        ? [`${enabledNames.join("、")} 还开着刷流，会一并关闭（否则引擎几分钟内又会拉新种）`]
        : []),
      ...(protectedCount > 0
        ? [
            `其中 ${protectedCount} 个（${formatBytes(sum((site) => site.protected_bytes))}）还没做满站点要求的做种时长，现在删可能被记 H&R，默认到期后自动删除${until ? `（最晚 ${until}）` : ""}`,
          ]
        : []),
    ];
    const options = {
      title: `清理 ${count} 个刷流种子？`,
      bullets,
      confirmLabel: enabledNames.length > 0 ? "关闭刷流并清理" : "清理",
      tone: "danger" as const,
    };
    let force = false;
    if (protectedCount > 0) {
      const result = await confirm({
        ...options,
        checkbox: {
          label: `保留期内的 ${protectedCount} 个也立即删除`,
          description: "可能被站点记 H&R（影响账号），只在确定不在乎时勾选。",
          defaultChecked: false,
        },
      });
      if (!result.ok) return;
      force = result.checked;
    } else if (!(await confirm(options))) {
      return;
    }
    setCleaning(true);
    try {
      const result = await cleanupBoostPool({ force });
      toast.success(boostCleanupSummary(result));
      onChanged();
      onPool(await getBoostPool());
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setCleaning(false);
    }
  }, [cleaning, confirm, onChanged, onPool, toast]);

  return { cleaning, cleanup };
}

/** 刷流单行的清理备注：已请求清理的种子标出何时自动删除 */
export function boostCleanupNote(state: BoostPoolTask | undefined): string | null {
  if (!state?.cleanup_scheduled) return null;
  const until = formatCleanupDeadline(state.protected_until);
  return until ? `已请求清理 · ${until} 保留期满后自动删除` : "已请求清理 · 下一轮巡检删除";
}

const MODE_TONE: Record<BoostMode, string> = {
  running: "text-[var(--ok)]",
  paused: "text-[var(--warn)]",
  off: "text-[var(--text-faint)]",
};

/**
 * 总览「进行中」分组末尾的刷流一行：按站点开关状态写文案（lib/activity-overview.ts 的
 * boostSummary），点进「刷流做种」二级页看逐站点、逐种子明细。
 */
export function BoostSummaryRow({ tasks, pool }: { tasks: DownloadTask[]; pool: BoostPool | null }) {
  const summary = boostSummary(tasks, pool);
  return (
    <GroupLinkRow href={"/activity?view=boost" as Route}>
      <span className={`grid w-6 shrink-0 place-items-center ${MODE_TONE[summary.mode]}`}>
        {summary.mode === "paused" ? (
          <ClockIcon className="size-[18px]" />
        ) : (
          <ArrowDownIcon className="size-[18px] rotate-180" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-ui font-semibold text-[var(--text)]">{summary.title}</span>
        <span className="tnum mt-0.5 line-clamp-2 block text-sub text-[var(--text-muted)]">
          {summary.detail}
        </span>
      </span>
    </GroupLinkRow>
  );
}

/** 刷流种子列表每批露出的条数：真正值得看的是正在出力的那些，其余按批「再显示」 */
const BOOST_PAGE_SIZE = 20;

/**
 * 「刷流做种」二级页（银玻璃手机）：页头实时汇总 → 按站点（刷流中 / 已暂停 / 已关闭）
 * → 逐种子一行；「清理」挂在右上角（种子动辄上百个，放列表底部要滑到头才找得到）。
 */
export function ActivityBoostPage() {
  const { boostTasks: tasks } = useTaskActivity();
  const { refresh } = useDownloadTasks();
  const { pool, setPool } = useBoostPool(tasks);
  const { cleaning, cleanup } = useBoostCleanup({ onPool: setPool, onChanged: refresh });
  const [visibleCount, setVisibleCount] = useState(BOOST_PAGE_SIZE);
  const totals = boostTotals(tasks);
  const sites = useMemo(() => boostSites(tasks, pool), [tasks, pool]);
  const sorted = useMemo(() => sortBoostTasks(tasks), [tasks]);

  // 清理入口挂到顶栏右上角（同 Safari 历史记录的「清除」），没有种子时不出现
  const setTopBarActions = usePageChrome()?.setTopBarActions;
  const hasTasks = tasks.length > 0;
  const cleanupButton = useMemo(
    () =>
      hasTasks ? (
        <button
          type="button"
          onClick={() => void cleanup()}
          disabled={cleaning}
          aria-label="清理刷流种子"
          className="shrink-0 rounded-full px-3 py-1.5 text-ui font-semibold text-[var(--danger)] transition active:opacity-60 disabled:opacity-50"
        >
          {cleaning ? "正在清理…" : "清理"}
        </button>
      ) : null,
    [cleaning, cleanup, hasTasks],
  );
  useEffect(() => {
    if (!setTopBarActions || !cleanupButton) return;
    return setTopBarActions(cleanupButton);
  }, [cleanupButton, setTopBarActions]);

  if (!hasTasks) {
    return (
      <p className="py-16 text-center text-ui text-[var(--text-muted)]">现在没有刷流种子在做种</p>
    );
  }

  return (
    <div>
      <ActivityGroup title={`${totals.count} 个种子`}>
        <div className="tnum grid grid-cols-2 gap-x-4 gap-y-1.5 px-3.5 py-3 text-sub text-[var(--text-muted)]">
          <SpeedStat direction="up" bytesPerSecond={totals.upSpeed} placeholder="↑ 0 B/s" />
          <SpeedStat direction="down" bytesPerSecond={totals.downSpeed} placeholder="↓ 0 B/s" />
          <span>
            已上传 <span className="font-semibold text-[var(--ok)]">{formatBytes(totals.uploaded)}</span>
          </span>
          <span>
            已下载 <span className="font-semibold text-[var(--info)]">{formatBytes(totals.downloaded)}</span>
          </span>
        </div>
      </ActivityGroup>

      {sites.sites.length > 0 && (
        <ActivityGroup
          title="按站点"
          footer={
            sites.count("off") > 0
              ? "关闭刷流不会删除已有种子：它们会继续满速做种，引擎也不再自动汰换。可点右上角「清理」删除。"
              : null
          }
        >
          {sites.sites.map((site) => {
            const size = site.tasks.reduce((total, task) => total + (task.size_bytes ?? 0), 0);
            const upSpeed = site.tasks.reduce((total, task) => total + (task.upspeed_bytes ?? 0), 0);
            const scheduled = site.pool?.scheduled_count ?? 0;
            return (
              <div key={site.id} className="flex items-center gap-3 px-3.5 py-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-ui font-semibold text-[var(--text)]">{site.name}</p>
                  <p className="tnum mt-0.5 text-sub text-[var(--text-muted)]">
                    {site.tasks.length} 个种子 · {formatBytes(size)} · ↑ {formatBytes(upSpeed)}/s
                  </p>
                  {scheduled > 0 && (
                    <p className="mt-0.5 text-sub text-[var(--warn)]">
                      {scheduled} 个已请求清理，保留期满后自动删除
                    </p>
                  )}
                </div>
                <span className={`shrink-0 text-sub font-medium ${MODE_TONE[site.mode]}`}>
                  {site.mode === "running" ? "刷流中" : site.mode === "paused" ? "已暂停" : "已关闭"}
                </span>
              </div>
            );
          })}
        </ActivityGroup>
      )}

      <ActivityGroup title="按上行速度排序">
        {sorted.slice(0, visibleCount).map((task) => (
          <BoostTaskRow
            key={task.id}
            task={task}
            cleanupNote={boostCleanupNote(sites.taskStates.get(task.info_hash.toLowerCase()))}
          />
        ))}
        {sorted.length > visibleCount && (
          <button
            type="button"
            onClick={() => setVisibleCount((count) => count + BOOST_PAGE_SIZE)}
            className="w-full px-3.5 py-3 text-center text-ui font-medium text-[var(--info)] transition active:opacity-60"
          >
            再显示 {Math.min(BOOST_PAGE_SIZE, sorted.length - visibleCount)} 个（共 {sorted.length} 个）
          </button>
        )}
      </ActivityGroup>
    </div>
  );
}

/**
 * 刷流单行（二级页）。先认得出是哪个种子，再看数字（TaskCards.swift 的 BoostTaskRow）：
 * 种子名独占整行、最多两行（按字符折行，种子名没有空格，按词折会在「WEB-」处提前断开）；
 * 站点、体积、累计上传收成一行小字；正在出力的上传速度靠右、绿色，静默种子不显示速度；
 * 下载中的少数种子再补一条进度条。整行可点，打开站点种子详情页。
 */
function BoostTaskRow({ task, cleanupNote }: { task: DownloadTask; cleanupNote: string | null }) {
  const downloading = task.state === "downloading";
  const percent = task.progress == null ? null : Math.floor(task.progress * 100);
  const upSpeed = task.upspeed_bytes ?? 0;
  const meta = [
    task.site_name,
    task.size_bytes != null ? formatBytes(task.size_bytes) : null,
    (task.uploaded_bytes ?? 0) > 0 ? `已上传 ${formatBytes(task.uploaded_bytes ?? 0)}` : null,
  ].filter(Boolean);
  const content = (
    <>
      <div className="flex items-baseline gap-2.5">
        <p className="line-clamp-2 min-w-0 flex-1 break-all text-sub text-[var(--text)]">
          {task.name || task.info_hash}
        </p>
        {upSpeed > 0 && (
          <span className="tnum shrink-0 text-caption font-semibold text-[var(--ok)]">
            ↑ {formatBytes(upSpeed)}/s
          </span>
        )}
      </div>
      <p className="tnum mt-1 truncate text-caption text-[var(--text-muted)]">{meta.join(" · ")}</p>
      {downloading && percent != null && (
        <div className="tnum mt-1.5 flex items-center gap-2 text-caption text-[var(--text-faint)]">
          <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-white/[0.08]">
            <div
              className="h-full rounded-full bg-[var(--info)]"
              style={{ width: `${Math.min(100, Math.max(1, percent))}%` }}
            />
          </div>
          <span>{percent}%</span>
          <SpeedStat direction="down" bytesPerSecond={task.dlspeed_bytes} />
        </div>
      )}
      {cleanupNote && <p className="mt-1 text-caption text-[var(--warn)]">{cleanupNote}</p>}
    </>
  );
  return task.page_url ? (
    <a
      href={task.page_url}
      target="_blank"
      rel="noreferrer"
      title="打开站点种子详情页"
      className="block px-3.5 py-2.5 transition active:opacity-70"
    >
      {content}
    </a>
  ) : (
    <div className="px-3.5 py-2.5">{content}</div>
  );
}
