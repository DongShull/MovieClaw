"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { ComponentType, ReactNode, SVGProps } from "react";

import { MoreIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS } from "@/components/page-nav";

export interface TopBarMenuItem {
  id: string;
  label: string;
  Icon?: ComponentType<SVGProps<SVGSVGElement>>;
  onSelect: () => void;
  tone?: "danger";
}

/**
 * 手机顶栏右上角的「⋯」菜单：与搜索键同一副圆形玻璃键，点开是一列带图标的菜单项。
 *
 * 对应原生 App 导航栏里的 `Menu { … } label: { Image(systemName: "ellipsis") }`——
 * 页面的低频动作收进一颗键，顶栏不再平铺一排图标（媒体库的「自定义首页 / 全部合集 /
 * 管理媒体库」即此，见 apps/apple/.../LibraryHomeView.swift）。
 * 页面经 usePageChrome().setTopBarActions 挂上来。
 */
export function TopBarMenu({
  label,
  items,
  icon,
}: {
  /** 触发键的读屏名 */
  label: string;
  items: TopBarMenuItem[];
  /** 触发键图标，缺省 ⋯ */
  icon?: ReactNode;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button type="button" aria-label={label} className={PAGE_NAV_BUTTON_CLASS}>
          {icon ?? <MoreIcon className="size-[22px]" />}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[11rem] p-1.5"
        >
          {items.map(({ id, label: text, Icon, onSelect, tone }) => (
            <DropdownMenu.Item
              key={id}
              onSelect={onSelect}
              className={`glass-row cursor-pointer px-2.5 py-2.5 text-ui font-medium outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)] ${
                tone === "danger" ? "!text-[var(--danger)]" : "text-[var(--text)]"
              }`}
            >
              {Icon && <Icon className="size-5 shrink-0 opacity-80" />}
              <span className="flex-1">{text}</span>
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
