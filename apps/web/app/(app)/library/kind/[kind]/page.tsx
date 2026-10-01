import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { KindWallView } from "@/components/kind-wall-view";
import type { HomeMediaKind } from "@/lib/api/libraries";

/** 兜底标题；视图内的 usePageTitle 会覆盖为「全部电影」等。 */
export const metadata: Metadata = { title: "媒体库" };

const KINDS: readonly HomeMediaKind[] = ["movie", "tv", "video"];

/**
 * 按类型的跨库海报墙（/library/kind/{movie|tv|video}）：首页「全部电影」行的
 * 「查看全部」落点。静态段 kind 优先于 /library/[id]，不会被当成库 id。
 */
export default async function KindWallPage({
  params,
}: {
  params: Promise<{ kind: string }>;
}) {
  const { kind } = await params;
  if (!KINDS.includes(kind as HomeMediaKind)) notFound();
  return (
    <div className="flex h-full flex-col">
      <KindWallView kind={kind as HomeMediaKind} />
    </div>
  );
}
