"use client";

/**
 * 「账号 → 通知」分区（docs/design/cloud-push.md §1、§5、§7.3、§8；交互稿 4.2）。
 *
 * 每个人只管自己收哪些手机通知，以及给自己发一条测试通知。设备只在「账号 → 设备」
 * 一个地方管：这里不列设备，只有自己有设备收不到时顶部出一条提示，点进设备页。
 * 成员只有这一个入口，不需要知道云、中继这些概念。
 *
 * 开关存在服务器上、按人一份，网页和 App 改的是同一份。服务器还没有任何可用
 * 通道时开关照样能改，顶部说明一句：成员看到「管理员还没有开启」，管理员看到
 * 去哪开启——开通后这里的设置立刻生效。
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Banner, ErrorBanner, LINK_CLASS, Toggle } from "@/components/cloud-push-ui";
import { useToast } from "@/components/feedback";
import {
  type MyPushView,
  getMyPush,
  sendMyPushTest,
  updateMyPushPreferences,
} from "@/lib/api/push";
import {
  attentionLine,
  groupEvents,
  summarizePushTest,
  testTargetHint,
} from "@/lib/cloud-push-display";

export function NotificationsSection() {
  const toast = useToast();
  const [view, setView] = useState<MyPushView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  // 只采纳最后一次改开关的响应：连着拨两个开关时，先回来的那份不能把后一个拨回去
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setView(await getMyPush());
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const setEventEnabled = (key: string, enabled: boolean) =>
    setView((prev) =>
      prev && {
        ...prev,
        events: prev.events.map((event) => (event.key === key ? { ...event, enabled } : event)),
      },
    );

  const toggle = async (key: string, enabled: boolean) => {
    const seq = ++seqRef.current;
    setError(null);
    setEventEnabled(key, enabled); // 乐观更新，失败回滚
    try {
      const next = await updateMyPushPreferences({ [key]: enabled });
      if (seq === seqRef.current) setView(next);
    } catch (e) {
      setEventEnabled(key, !enabled);
      setError((e as Error).message);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const summary = summarizePushTest(await sendMyPushTest());
      if (summary.tone === "success") toast.success(summary.message);
      else toast.error(summary.message);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setTesting(false);
    }
  };

  if (view == null) {
    return loadError ? (
      <div className="space-y-3">
        <ErrorBanner>{loadError}</ErrorBanner>
        <button
          type="button"
          onClick={() => void load()}
          className="btn-glass px-4 py-1.5 text-sub font-medium"
        >
          重试
        </button>
      </div>
    ) : (
      <div className="space-y-2.5">
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
        <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {!view.instance_ready &&
        (view.is_admin ? (
          <Banner
            tone="info"
            title="这台服务器还没有开启手机通知"
            action={
              <Link
                href="/settings/cloud"
                className="btn-accent inline-flex rounded-full px-4 py-1.5 text-sub font-semibold"
              >
                去开启
              </Link>
            }
          >
            连接 MovieClaw Cloud 就能用官方推送；自己打包的 App 可以在{" "}
            <Link href="/settings/app-push" className={LINK_CLASS}>
              App 推送
            </Link>{" "}
            里添加自建中继。下面的开关现在就能改，开启后立刻生效。
          </Banner>
        ) : (
          <Banner tone="info">管理员还没有开启手机通知，开启后这里的设置会立刻生效。</Banner>
        ))}

      {/* 自己有设备收不到才提示，逐台一句原因；设备的全貌在「设备」页 */}
      {view.attention.length > 0 && (
        <Banner
          tone="warn"
          title={`有 ${view.attention.length} 台设备收不到通知`}
          action={
            <Link href="/settings/devices" className="btn-glass inline-flex px-3 py-1.5 text-sub font-medium">
              查看设备
            </Link>
          }
        >
          <ul>
            {view.attention.map((item) => (
              <li key={item.device_id}>{attentionLine(item)}</li>
            ))}
          </ul>
        </Banner>
      )}

      {error && <ErrorBanner>{error}</ErrorBanner>}

      {groupEvents(view.events).map((group) => (
        <section key={group.group}>
          <h3 className="group-label mb-2.5 px-1">{group.group}</h3>
          <div className="css-glass !rounded-xl">
            {group.items.map((event, i) => (
              <div
                key={event.key}
                className={`flex items-center gap-3.5 p-4 ${i > 0 ? "border-t border-white/[0.06]" : ""}`}
              >
                <div className="min-w-0 flex-1">
                  <p className="text-body font-medium text-[var(--text)]">{event.title}</p>
                  {event.description && (
                    <p className="mt-0.5 text-caption leading-5 text-[var(--text-faint)]">
                      {event.description}
                    </p>
                  )}
                </div>
                <Toggle
                  checked={event.enabled}
                  label={`${event.title}通知`}
                  onChange={(next) => void toggle(event.key, next)}
                />
              </div>
            ))}
          </div>
        </section>
      ))}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-1">
        <button
          type="button"
          disabled={testing || view.ready_devices === 0}
          onClick={() => void sendTest()}
          className="btn-glass px-4 py-1.5 text-sub font-medium disabled:opacity-40"
        >
          {testing ? "发送中…" : "给我的设备发一条测试通知"}
        </button>
        <p className="text-caption text-[var(--text-faint)]">{testTargetHint(view.ready_devices)}</p>
      </div>
    </div>
  );
}

