"use client";

import { Children, type ReactNode } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ChevronRightIcon } from "@/components/icons";

/**
 * 活动总览（银玻璃手机）与其二级页共用的「分组」：对应原生 App 的系统分组列表
 * （insetGrouped List + ActivitySectionHeader，见 ActivityDashboardRows.swift）。
 *
 *     ● 需要处理 2 ………………………… 全部忽略
 *     ┌────────────────────────────────┐
 *     │ 行                              │
 *     ├────────────────────────────────┤
 *     │ 行                              │
 *     └────────────────────────────────┘
 *       脚注（浏览范围、说明）
 *
 * 标题行的圆点只给「此刻」分组：需要处理（红）与正在播放（绿）；右侧的「查看全部 ›」
 * 去往二级页（带箭头），「全部忽略」这类就地动作不带箭头。
 */
export function ActivityGroup({
  title,
  count,
  tone,
  trailing,
  footer,
  plain = false,
  children,
}: {
  title: string;
  count?: number | null;
  /** 标题前的圆点：danger（需要处理）/ ok（正在播放）；其余分组不带 */
  tone?: "danger" | "ok";
  trailing?: GroupTrailing | null;
  footer?: ReactNode;
  /** 子节点自带卡片外观（需要处理的完整卡片），不再包分组容器 */
  plain?: boolean;
  children?: ReactNode;
}) {
  return (
    <section className="mt-6 first:mt-0" aria-label={title}>
      <div className="mb-2 flex items-center gap-1.5 px-1">
        {tone && (
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${
              tone === "danger" ? "bg-[var(--danger)]" : "bg-[var(--ok)]"
            }`}
          />
        )}
        <h2 className="text-body font-semibold text-[var(--text)]">{title}</h2>
        {count != null && <span className="tnum text-body text-[var(--text-faint)]">{count}</span>}
        {trailing && <GroupTrailingButton {...trailing} />}
      </div>
      {/* toArray 会滤掉 null / false：只有折叠计数（没有可列的行）时不画空容器 */}
      {Children.toArray(children).length > 0 &&
        (plain ? (
          <div className="space-y-2.5">{children}</div>
        ) : (
          <div className="divide-y divide-white/[0.06] overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.03]">
            {children}
          </div>
        ))}
      {footer && (
        <div className="mt-2 px-1 text-caption leading-5 text-[var(--text-faint)]">{footer}</div>
      )}
    </section>
  );
}

/** 分组标题右侧的入口：href 去往二级页（带箭头）；onClick 是就地动作（不带箭头） */
export type GroupTrailing =
  | { label: string; href: Route; onClick?: never; disabled?: never }
  | { label: string; onClick: () => void; href?: never; disabled?: boolean };

function GroupTrailingButton(trailing: GroupTrailing) {
  const className =
    "ml-auto flex shrink-0 items-center gap-0.5 py-1.5 pl-3 text-sub text-[var(--text-muted)] transition hover:text-white disabled:opacity-40";
  if (trailing.href) {
    return (
      <Link href={trailing.href} className={className}>
        {trailing.label}
        <ChevronRightIcon className="size-3.5" />
      </Link>
    );
  }
  return (
    <button
      type="button"
      onClick={trailing.onClick}
      disabled={trailing.disabled}
      className={className}
    >
      {trailing.label}
    </button>
  );
}

/**
 * 分组里可点的一行：整行去往某处（二级页 / 详情），右侧可再挂一个 ⋯ 菜单。
 * 菜单不能嵌在链接里（按钮套在 <a> 里点菜单会连带触发跳转），所以链接只包主体、菜单并排。
 */
export function GroupLinkRow({
  href,
  menu,
  children,
}: {
  href: Route | null;
  menu?: ReactNode;
  children: ReactNode;
}) {
  const body = href ? (
    <Link
      href={href}
      className="flex min-w-0 flex-1 items-center gap-3 py-3 pl-3.5 transition active:opacity-70"
    >
      {children}
      {!menu && <ChevronRightIcon className="mr-3 size-4 shrink-0 text-white/25" />}
    </Link>
  ) : (
    <div className="flex min-w-0 flex-1 items-center gap-3 py-3 pl-3.5 pr-3.5">{children}</div>
  );
  return (
    <div className="flex min-w-0 items-center">
      {body}
      {menu && <div className="shrink-0 pl-1 pr-2.5">{menu}</div>}
    </div>
  );
}
