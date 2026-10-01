"use client";

import { useEffect, useMemo, useState } from "react";

import { listLibraries } from "@/lib/api/libraries";
import type { SearchVertical } from "@/lib/categories";
import { usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";

const ORDERED_SEARCH_VERTICALS: SearchVertical[] = ["media", "torrent", "library"];

export interface SearchAccess {
  canMedia: boolean;
  canTorrent: boolean;
  canLibrary: boolean;
  ready: boolean;
  available: SearchVertical[];
  firstAvailable: SearchVertical | null;
  /** 搜索入口（按钮 / ⌘K）是否露出：任一分区可用即可（媒体库分区探测完成前按不可用算）。 */
  canOpenSearch: boolean;
}

// 「成员有没有可见库」的探测结果在模块级共享：侧栏、顶栏、搜索面板等多处同时挂
// useSearchAccess，各自请求 /libraries 纯属浪费。按账号区分（切换账号不复用），
// 30 秒后过期重探，让超管调整可见库后成员这边不至于长期拿着旧结论。
const LIBRARY_PROBE_TTL_MS = 30_000;
let libraryProbe: { username: string; at: number; promise: Promise<boolean> } | null = null;
// 最近一次探测成功的结论：新挂载的组件（如进出详情页后重挂的顶栏）先同步用它出首帧，
// 再在后台按 TTL 复核——否则每次重挂都要先藏一帧搜索键、等 Promise 回来再露出（闪烁）
let lastKnown: { username: string; available: boolean } | null = null;

function knownLibraryAvailable(username: string): boolean | null {
  return lastKnown?.username === username ? lastKnown.available : null;
}

function probeLibraryAvailable(username: string): Promise<boolean> {
  const now = Date.now();
  if (
    libraryProbe &&
    libraryProbe.username === username &&
    now - libraryProbe.at <= LIBRARY_PROBE_TTL_MS
  ) {
    return libraryProbe.promise;
  }
  const promise = listLibraries().then(
    (libraries) => {
      const available = libraries.length > 0;
      lastKnown = { username, available };
      return available;
    },
    () => {
      // 失败不缓存，下次挂载重试（只清自己这一次，别误删期间新发起的探测）
      if (libraryProbe?.promise === promise) libraryProbe = null;
      // 偶发失败不推翻已知结论（否则复核时网络一抖搜索键就消失）
      return knownLibraryAvailable(username) ?? false;
    },
  );
  libraryProbe = { username, at: now, promise };
  return promise;
}

/**
 * 搜索入口的前端权限快照。
 *
 * - 影视：对应成员「订阅」能力，能订阅才需要查影视条目；
 * - 站点资源：对应成员「PT 站资源搜索」能力；
 * - 媒体库：对应成员可见媒体库白名单，至少有一个可见库才展示。
 *
 * 后端仍是最终边界；这里负责菜单/按钮不展示不可用入口，减少误点。
 */
export function useSearchAccess(): SearchAccess {
  const permissions = usePermissions();
  const username = useSession().session.username;
  const [libraryAvailable, setLibraryAvailable] = useState<boolean | null>(() =>
    permissions.isAdmin ? true : knownLibraryAvailable(username),
  );

  useEffect(() => {
    if (permissions.isAdmin) {
      setLibraryAvailable(true);
      return;
    }
    let cancelled = false;
    // 有同账号的旧结论就先沿用（后台复核），没有才回到「探测中」
    setLibraryAvailable(knownLibraryAvailable(username));
    void probeLibraryAvailable(username).then((available) => {
      if (!cancelled) setLibraryAvailable(available);
    });
    return () => {
      cancelled = true;
    };
  }, [permissions.isAdmin, username]);

  const canMedia = permissions.canSubscribe;
  const canTorrent = permissions.canSearch;
  const canLibrary = permissions.isAdmin || libraryAvailable === true;
  const ready = permissions.isAdmin || libraryAvailable !== null;

  return useMemo(() => {
    const available = ORDERED_SEARCH_VERTICALS.filter((vertical) => {
      if (vertical === "media") return canMedia;
      if (vertical === "torrent") return canTorrent;
      return canLibrary;
    });
    return {
      canMedia,
      canTorrent,
      canLibrary,
      ready,
      available,
      firstAvailable: available[0] ?? null,
      canOpenSearch: available.length > 0,
    };
  }, [canLibrary, canMedia, canTorrent, ready]);
}
