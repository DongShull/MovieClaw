"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { LockIcon, UserIcon } from "@/components/icons";
import {
  WelcomeCard,
  WelcomeError,
  WelcomeField,
  WelcomeFields,
  WelcomeScreen,
  WelcomeSubmit,
} from "@/components/welcome-screen";
import { createAdmin, getBootstrapStatus } from "@/lib/api/auth";
import { usePageTitle } from "@/lib/use-page-title";
import { HttpError } from "@/lib/http";

/**
 * 首次初始化引导页：创建超级管理员账号（全生命周期只此一次）。
 *
 * 与登录页同一张星空欢迎页（components/welcome-screen.tsx）：首页按钮「开始使用」升起
 * 「初始化这台服务器」卡片（文案同原生 App 遇到全新服务器时的卡片）。
 *
 * 挂载时校验初始化状态：已初始化则立即转登录页——这只是防误入的导航，
 * 真正的"只能初始化一次"由后端一次性锁保证（重复提交必得 409），
 * 改前端代码绕不过去。
 */
export default function SetupPage() {
  usePageTitle("初始化");
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getBootstrapStatus()
      .then((status) => {
        if (cancelled) return;
        if (status.initialized) router.replace("/login");
        else setReady(true);
      })
      .catch(() => {
        // 状态查询失败（后端未起）：留在引导页，提交时自然会报错
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  return (
    <WelcomeScreen
      buttonLabel="开始使用"
      ready={ready}
      card={({ close, autoFocus }) => <SetupCard autoFocus={autoFocus} onClose={close} />}
    />
  );
}

function SetupCard({ autoFocus, onClose }: { autoFocus: boolean; onClose: () => void }) {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);

  // 等卡片升起后再聚焦（同 App：动画与弹键盘挤在一起会卡）
  useEffect(() => {
    if (!autoFocus) return;
    const timer = window.setTimeout(() => usernameRef.current?.focus(), 450);
    return () => window.clearTimeout(timer);
  }, [autoFocus]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;

    const name = username.trim();
    if (name.length < 3) {
      setError("用户名至少 3 个字符");
      return;
    }
    if (password.length < 8) {
      setError("密码至少 8 位，建议混用字母与数字");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }

    setError(null);
    setBusy(true);
    try {
      await createAdmin(name, password);
      // 建号即自动登录，整页进入首页让应用在已登录态下初始化
      window.location.href = "/";
    } catch (err) {
      if (err instanceof HttpError && err.status === 409) {
        // 一次性锁已闭合（可能在另一个标签页里完成了初始化）
        router.replace("/login");
        return;
      }
      setError(err instanceof HttpError ? err.message : "网络异常，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <WelcomeCard
      title="初始化这台服务器"
      subtitle="这是一台全新的服务器。将用下面的账号创建超级管理员——它是本站唯一的管理身份，此流程仅在首次部署时出现。"
      onClose={onClose}
    >
      <form onSubmit={submit} className="space-y-4">
        <WelcomeFields>
          <WelcomeField
            ref={usernameRef}
            icon={<UserIcon className="size-[18px]" />}
            placeholder="管理员用户名"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="next"
          />
          <WelcomeField
            icon={<LockIcon className="size-[18px]" />}
            placeholder="密码（至少 8 位）"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            enterKeyHint="next"
          />
          <WelcomeField
            icon={<LockIcon className="size-[18px]" />}
            placeholder="确认密码"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            enterKeyHint="go"
          />
        </WelcomeFields>
        <WelcomeError message={error} />
        <WelcomeSubmit busy={busy} disabled={busy || !username.trim() || !password || !confirm}>
          {busy ? "创建中…" : "创建账号并进入"}
        </WelcomeSubmit>
      </form>
    </WelcomeCard>
  );
}
