"""AI 字幕生成端到端演练：真实后端 + 真实 ffmpeg + 假模型网关，把整条链路真跑一遍。

iOS App 与网页走的是同一组接口（预检 → 发起 → 跟任务进度），这里用 HTTP 客户端
照同样的顺序调用，逐项核对用户能感知到的体验与可靠性：

- S1 主流程：内封 SRT 还没读取过。预检、发起都要秒回；任务第一步读取字幕并报
  「已读到 hh:mm:ss」；存储按固定字节速率限速（模拟 NAS 慢读）；网关同时注入
  限流风暴、输出截断、坏 JSON 与超长译文，任务仍要成功、时间轴一条不错位。
- S2 双语 + 崩溃恢复：参考字幕已读过，预检给精确条数；翻译到一半 kill -9 后端，
  重启后任务按租约自愈、从断点续传，已完成的块不再重复花钱。
- S3 读取排队 + 读取中停止 + 不同步拦截：两部没读过的片子同时生成，后一部报
  「排队等待读取」；停止正在读取的那部要秒级生效且 ffmpeg 整组结束；另一部接着
  读完，因参考字幕与音轨不同步在调用模型之前停下，一分钱不花。
- S4 上游故障：翻译中模型服务持续 503，任务自动重试并从断点续传。

不进 CI，本机手动跑（需要 ffmpeg / ffprobe）：
    .venv/bin/python scripts/perf/e2e_subtitle_gen.py
运行数据落在系统临时目录；最后打印报告，任一检查不通过即非零退出。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
RUN = Path(tempfile.mkdtemp(prefix="movieclaw-subgen-e2e-"))
# 素材编码要一分多钟：生成一次缓存在系统临时目录，反复演练时直接复用
MEDIA_CACHE = Path(tempfile.gettempdir()) / "movieclaw-subgen-e2e-media-v1"
PY = sys.executable
API_PORT, GATEWAY_PORT = 18811, 18812
API = f"http://127.0.0.1:{API_PORT}/api/v1"
GATEWAY = f"http://127.0.0.1:{GATEWAY_PORT}"
REAL_FFMPEG = shutil.which("ffmpeg")

RUNTIME_SECONDS = 2 * 3600  # 两小时的片子
PERIOD = 5  # 第 k 句台词在 [5k+1, 5k+3] 秒；音轨在同样的区间里「有人说话」
READ_SECONDS = 25  # 限速后通读一部片子大约要这么久

# ffmpeg 垫片：整文件抽字幕（带 -progress 与 -map 0:s:）时，把输入换成按字节限速
# 的管道，像真实 NAS 那样一点点读；其余命令（抽音频、探测）原样交给真 ffmpeg。
SHIM = r"""#!{python}
import os, subprocess, sys, threading, time
REAL = {real!r}
argv = sys.argv[1:]
rate = float(os.environ.get("E2E_READ_BYTES_PER_SEC") or 0)
if rate > 0 and "-progress" in argv and "-i" in argv and any(a.startswith("0:s:") for a in argv):
    i = argv.index("-i")
    src, argv[i + 1] = argv[i + 1], "pipe:0"
    proc = subprocess.Popen([REAL, *argv], stdin=subprocess.PIPE)

    def feed():
        start, sent = time.monotonic(), 0
        try:
            with open(src, "rb") as fh:
                while data := fh.read(65536):
                    proc.stdin.write(data)
                    sent += len(data)
                    ahead = sent / rate - (time.monotonic() - start)
                    if ahead > 0:
                        time.sleep(ahead)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    sys.exit(proc.wait())
os.execv(REAL, [REAL, *argv])
"""

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"    {'✓' if ok else '✗'} {name}" + (f"（{detail}）" if detail else ""))


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------
# 素材：两小时的 MKV，内封英语 SRT（1440 句）+ 中文 SRT，音轨与台词同节奏
# ---------------------------------------------------------------------------


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


LINES = [
    "Neo, you have to wake up now.",
    "I told you we should never have come here.",
    "Is anybody out there?",
    "We are running out of time.",
    "Follow the white rabbit.",
]


def _write_srt(path: Path, *, offset: float = 0.0, every: int = 1, text=None) -> int:
    cues = []
    for k in range(0, RUNTIME_SECONDS // PERIOD, every):
        start = k * PERIOD + 1 + offset
        body = text(k) if text else f"{LINES[k % len(LINES)]} ({k})"
        cues.append(f"{len(cues) + 1}\n{_ts(start)} --> {_ts(start + 2)}\n{body}\n")
    path.write_text("\n".join(cues), encoding="utf-8")
    return len(cues)


def make_movie(path: Path, *, offset: float = 0.0) -> None:
    work = MEDIA_CACHE / "work"
    work.mkdir(parents=True, exist_ok=True)
    eng, chi, beat = work / "eng.srt", work / "chi.srt", work / "beat.wav"
    _write_srt(eng, offset=offset)
    _write_srt(chi, every=12, text=lambda k: f"中文字幕第{k}句")
    if not beat.exists():
        # 5 秒一个节拍：1~3 秒是「说话」（300 Hz 正弦），其余静音
        subprocess.run(
            [
                REAL_FFMPEG,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc='0.5*sin(2*PI*300*t)*between(mod(t\\,5)\\,1\\,3)':s=16000:d=5",
                str(beat),
            ],
            check=True,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            REAL_FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=2",
            "-stream_loop",
            "-1",
            "-i",
            str(beat),
            "-i",
            str(eng),
            "-i",
            str(chi),
            "-t",
            str(RUNTIME_SECONDS),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-map",
            "3:s",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "20k",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:0",
            "title=English",
            "-metadata:s:s:1",
            "language=chi",
            "-metadata:s:s:1",
            "title=简体中文",
            str(path),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# 进程：假网关 + 真后端
# ---------------------------------------------------------------------------


def backend_env(read_rate: float) -> dict[str, str]:
    shim_dir = RUN / "shim"
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{RUN}/data/e2e.db",
        "SECRET_KEY_FILE": str(RUN / "data" / ".secret_key"),
        "AGENT_SESSIONS_DIR": str(RUN / "data" / "agent-sessions"),
        "SCHEDULER_ENABLED": "0",
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "E2E_READ_BYTES_PER_SEC": str(read_rate),
    }


def start_api(env: dict[str, str], log_name: str) -> subprocess.Popen:
    out = open(RUN / log_name, "w")  # noqa: SIM115 -- 句柄随子进程存续
    proc = subprocess.Popen(
        [
            PY,
            "-m",
            "uvicorn",
            "movieclaw_api.main:app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        cwd=RUN,
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{API}/health", timeout=2).status_code == 200:
                return proc
        if proc.poll() is not None:
            sys.exit(f"后端提前退出，见 {RUN / log_name}")
        time.sleep(0.3)
    sys.exit("后端没有在期限内就绪")


def gateway(**control) -> None:
    httpx.post(f"{GATEWAY}/control", json=control, timeout=5).raise_for_status()


def gateway_stats() -> dict:
    return httpx.get(f"{GATEWAY}/stats", timeout=5).json()


# ---------------------------------------------------------------------------
# 客户端动作（与 iOS / 网页同一组接口）
# ---------------------------------------------------------------------------


def login(client: httpx.Client) -> None:
    r = client.post(f"{API}/auth/login", json={"username": "admin", "password": "e2e-pass-1"})
    assert r.status_code == 200, r.text


def timed(call):
    started = time.monotonic()
    response = call()
    return response, (time.monotonic() - started) * 1000


def preview(client: httpx.Client, file_id: int, target: str, secondary: str | None = None):
    params = {"target_language": target}
    if secondary:
        params["secondary_language"] = secondary
    r, ms = timed(
        lambda: client.get(
            f"{API}/libraries/files/{file_id}/subtitles/generation-preview", params=params
        )
    )
    assert r.status_code == 200, r.text
    return r.json()["data"], ms


def start(client: httpx.Client, file_id: int, target: str, secondary: str | None = None):
    body = {"target_language": target, "secondary_language": secondary}
    r, ms = timed(
        lambda: client.post(f"{API}/libraries/files/{file_id}/subtitles/generations", json=body)
    )
    assert r.status_code == 202, r.text
    return r.json()["data"], ms


class JobWatch:
    """用长轮询跟一个任务，按 revision 记下每一次状态/进度变化（相当于 App 上看到的）。"""

    def __init__(self, client: httpx.Client, job: dict) -> None:
        self.client = client
        self.job = job
        self.started = time.monotonic()
        self.timeline: list[tuple[float, str, str, float | None, str]] = []
        self._note(job)

    def _note(self, job: dict) -> None:
        progress = job["progress"]
        entry = (
            round(time.monotonic() - self.started, 1),
            job["status"],
            progress.get("phase") or "",
            progress.get("percent"),
            progress.get("message") or "",
        )
        if not self.timeline or self.timeline[-1][1:] != entry[1:]:
            self.timeline.append(entry)

    def poll(self, timeout: float = 5) -> dict:
        r = self.client.get(
            f"{API}/jobs/{self.job['id']}/wait",
            params={"after_revision": self.job["revision"], "wait_seconds": timeout},
            timeout=timeout + 10,
        )
        assert r.status_code == 200, r.text
        self.job = r.json()["data"]["job"]
        self._note(self.job)
        return self.job

    def until(self, predicate, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.job):
                return self.job
            with contextlib.suppress(httpx.TransportError):
                self.poll()
        raise AssertionError(f"等待超时，任务停在：{self.job['status']} / {self.job['progress']}")

    def phases(self) -> list[str]:
        seen: list[str] = []
        for _t, _status, phase, _pct, _msg in self.timeline:
            if phase and (not seen or seen[-1] != phase):
                seen.append(phase)
        return seen

    def messages(self, phase: str) -> list[str]:
        return [msg for _t, _s, p, _pct, msg in self.timeline if p == phase]


def finished(job: dict) -> bool:
    return job["status"] in {"succeeded", "failed", "cancelled", "blocked"}


def read_srt(path: Path) -> list[tuple[str, str]]:
    blocks = [b for b in path.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()]
    out = []
    for block in blocks:
        lines = block.splitlines()
        out.append((lines[1], "\n".join(lines[2:])))
    return out


def ffmpeg_readers(video: Path) -> list[str]:
    """命令行里带着这个视频路径的进程（抽取垫片与 ffmpeg）。

    不用 ``pgrep -f``：它把参数当正则，片名里的括号会被当成分组而匹配不到。
    """
    needle = str(video).encode()
    pids = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        with contextlib.suppress(OSError):
            if needle in cmdline.read_bytes():
                pids.append(cmdline.parent.name)
    return pids


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def scenario_main(client: httpx.Client, file_id: int, video: Path) -> None:
    log("S1 主流程：内封字幕没读过 + NAS 慢读 + 限流风暴 / 截断 / 坏 JSON / 超长译文")
    gateway(
        reset_stats=True,
        latency=0.15,
        rate_limit_after=12,
        rate_limit_seconds=5,
        truncate_over=40,
        garbage_every=13,
        long_every=97,
        error_after=None,
    )
    pv, preview_ms = preview(client, file_id, "chs")
    check("预检秒回", preview_ms < 1000, f"{preview_ms:.0f} ms")
    check(
        "没读过的内封字幕照常选中，不报错",
        pv["chosen_key"] == "embedded:0" and pv["blocker"] is None,
    )
    check(
        "给出读取说明与片长粗估",
        bool(pv["reference_notice"]) and pv["estimated_tokens"] > 0,
        f"粗估 {pv['estimated_tokens']} token；{pv['reference_notice']}",
    )
    job, start_ms = start(client, file_id, "chs")
    check("点「确认生成」秒回并入队", start_ms < 1000, f"{start_ms:.0f} ms")
    watch = JobWatch(client, job)
    watch.until(finished, timeout=600)
    job = watch.job
    reading = watch.messages("extracting")
    positions = [m for m in reading if "已读到" in m]
    check(
        "读取阶段报出「已读到 hh:mm:ss / 片长」",
        len(positions) >= 3,
        f"{len(positions)} 次，如「{positions[len(positions) // 2] if positions else '无'}」",
    )
    percents = [pct for _t, _s, phase, pct, _m in watch.timeline if phase == "extracting" and pct]
    check(
        "读取进度单调前进",
        percents == sorted(percents) and len(percents) >= 3,
        f"{percents[:3]} … {percents[-2:]}" if percents else "无",
    )
    phases = watch.phases()
    # 进度按 0.75 秒一次落库，一闪而过的阶段（术语表、写盘）可能不在快照里；
    # 核对的是关键顺序：先读取、再同步检查、再翻译，最后成功
    ordered = all(name in phases for name in ("extracting", "syncing", "translating")) and (
        phases.index("extracting") < phases.index("syncing") < phases.index("translating")
    )
    check(
        "先读取、再同步检查、再翻译，最后成功",
        ordered and job["status"] == "succeeded",
        " → ".join(phases),
    )
    details = job["progress"]["details"]
    stats = gateway_stats()
    requests = stats["requests"]
    check(
        "限流风暴被降速重试吸收",
        details["rate_limit_count"] > 0 and requests.get("translate:429", 0) > 0,
        f"429 共 {requests.get('translate:429', 0)} 次，任务记到 {details['rate_limit_count']} 次",
    )
    check(
        "输出截断后对半拆块重译",
        requests.get("translate:truncated", 0) > 0,
        f"{requests.get('translate:truncated', 0)} 次截断",
    )
    check(
        "坏 JSON 被结构校验拦下并重试",
        requests.get("translate:garbage", 0) > 0 and details["validation_retries"] > 0,
        f"校验重试 {details['validation_retries']} 次",
    )
    check("超读速译文触发压缩", requests.get("compress:ok", 0) > 0)
    out = video.with_name(f"{video.stem}.ai-chs.chi.srt")
    # 对照片内那条字幕轨本身（封装时音频预滚会把整条轨平移几十毫秒，所以不拿
    # 生成素材用的原始 SRT 比）：直接用 ffmpeg 抽出来
    reference = RUN / "work-reference.srt"
    subprocess.run(
        [
            REAL_FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-map",
            "0:s:0",
            "-c:s",
            "srt",
            str(reference),
        ],
        check=True,
    )
    source = read_srt(reference)
    cues = read_srt(out) if out.exists() else []
    aligned = len(cues) == len(source) and all(
        cue_time == src_time and cue_text.startswith(f"译文{index}号")
        for index, ((cue_time, cue_text), (src_time, _)) in enumerate(
            zip(cues, source, strict=True)
        )
    )
    check("产物条数、时间轴与原字幕逐条一致", aligned, f"{out.name}：{len(cues)} 条")
    return watch


def scenario_bilingual_crash(client: httpx.Client, file_id: int, api_env: dict) -> subprocess.Popen:
    log("S2 双语 + 崩溃恢复：翻译到一半 kill -9 后端")
    gateway(
        reset_stats=True,
        latency=2.5,
        rate_limit_after=None,
        truncate_over=None,
        garbage_every=None,
        long_every=None,
        error_after=None,
    )
    pv, preview_ms = preview(client, file_id, "chs", "eng")
    check(
        "参考字幕读过后预检给精确条数",
        pv["reference_notice"] is None and pv["event_count"] == 1440,
        f"{pv['event_count']} 条 · 约 {pv['estimated_tokens']} token，{preview_ms:.0f} ms",
    )
    job, _ = start(client, file_id, "chs", "eng")
    watch = JobWatch(client, job)
    watch.until(lambda j: (j["progress"]["details"].get("done_blocks") or 0) >= 10, timeout=300)
    done_before = watch.job["progress"]["details"]["done_blocks"]
    api = SCENARIO_STATE["api"]
    os.kill(api.pid, signal.SIGKILL)
    api.wait()
    log(f"    已 kill -9 后端（此时完成 {done_before}/29 块），重启中")
    api = start_api(api_env, "api-after-crash.log")
    SCENARIO_STATE["api"] = api
    login(client)
    watch.client = client
    crash_at = time.monotonic()
    watch.until(finished, timeout=300)
    check(
        "重启后任务按租约自愈并完成",
        watch.job["status"] == "succeeded",
        f"重启后 {time.monotonic() - crash_at:.0f} 秒完成，第 {watch.job['attempt']} 次执行",
    )
    stats = gateway_stats()
    in_flight_bound = 8 * 50
    check(
        "已完成的块不重复花钱（只有在途块重翻）",
        stats["duplicate_lines"] <= in_flight_bound,
        f"重复交付 {stats['duplicate_lines']} 条（在途上限 {in_flight_bound} 条），"
        f"总交付 {stats['delivered_lines']} 条",
    )
    video = Path(SCENARIO_STATE["videos"]["main"])
    out = video.with_name(f"{video.stem}.ai-bilingual-chs-eng.chi.srt")
    cues = read_srt(out) if out.exists() else []
    two_lines = bool(cues) and all(len(text.splitlines()) == 2 for _t, text in cues)
    check("双语产物每条固定两行", two_lines and len(cues) == 1440, f"{out.name}：{len(cues)} 条")
    return api


def scenario_queue_cancel_offsync(
    client: httpx.Client, cancel_id: int, cancel_video: Path, offsync_id: int
) -> None:
    log("S3 读取排队 + 读取中停止 + 不同步拦截")
    gateway(reset_stats=True, latency=0.1)
    first, _ = start(client, cancel_id, "chs")
    second, _ = start(client, offsync_id, "chs")
    watch_first, watch_second = JobWatch(client, first), JobWatch(client, second)
    watch_first.until(lambda j: "已读到" in (j["progress"].get("message") or ""), timeout=60)
    watch_second.until(lambda j: j["progress"]["details"].get("extract_queued") is True, timeout=30)
    check(
        "后一部报「排队等待读取」",
        "排队等待读取" in watch_second.job["progress"]["message"],
        watch_second.job["progress"]["message"],
    )
    readers = ffmpeg_readers(cancel_video)
    check("正在读取的片子有 ffmpeg 在读", bool(readers), f"pid {readers}")
    r, cancel_ms = timed(lambda: client.post(f"{API}/jobs/{first['id']}/cancel"))
    assert r.status_code == 200, r.text
    stop_at = time.monotonic()
    watch_first.until(finished, timeout=60)
    stop_seconds = time.monotonic() - stop_at
    check(
        "读取中点停止秒级生效",
        watch_first.job["status"] == "cancelled" and stop_seconds < 5,
        f"{stop_seconds:.1f} 秒到「已取消」",
    )
    time.sleep(1)
    check(
        "停止后 ffmpeg 整组结束，不留孤儿",
        not ffmpeg_readers(cancel_video),
        f"剩余 {ffmpeg_readers(cancel_video)}",
    )
    watch_second.until(finished, timeout=300)
    job = watch_second.job
    message = (job.get("error") or {}).get("message") or ""
    check("排到的片子接着读完", "extracting" in watch_second.phases())
    check(
        "字幕与音轨不同步时在调用模型前停下",
        job["status"] == "failed" and "不同步" in message,
        message[:60],
    )
    translate_calls = gateway_stats()["translate_requests"]
    check("不同步的片子一次翻译请求都没发", translate_calls == 0, f"{translate_calls} 次")


def scenario_upstream_outage(client: httpx.Client, file_id: int) -> None:
    log("S4 上游故障：翻译中模型服务持续 503")
    gateway(reset_stats=True, latency=0.5, error_after=6, error_seconds=8)
    job, _ = start(client, file_id, "cht")
    watch = JobWatch(client, job)
    watch.until(finished, timeout=300)
    statuses = [status for _t, status, *_ in watch.timeline]
    stats = gateway_stats()
    check(
        "遇到持续 503 时任务自动重试",
        "retry_wait" in statuses,
        f"503 共 {stats['requests'].get('translate:503', 0)} 次",
    )
    check(
        "重试后从断点续传并完成",
        watch.job["status"] == "succeeded",
        f"第 {watch.job['attempt']} 次执行成功，重复交付 {stats['duplicate_lines']} 条",
    )
    retry_messages = [msg for _t, status, _p, _pct, msg in watch.timeline if status == "retry_wait"]
    check(
        "等待重试时说清是模型服务的问题",
        any("模型服务暂时不可用" in msg for msg in retry_messages),
        f"用户看到：「{retry_messages[0][:90] if retry_messages else '无'}」",
    )


SCENARIO_STATE: dict = {}


def main() -> None:
    if REAL_FFMPEG is None or shutil.which("ffprobe") is None:
        sys.exit("需要本机安装 ffmpeg 与 ffprobe")
    (RUN / "data").mkdir(parents=True)
    log(f"运行目录：{RUN}")
    shim_dir = RUN / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "ffmpeg"
    shim.write_text(SHIM.format(python=PY, real=REAL_FFMPEG), encoding="utf-8")
    shim.chmod(0o755)

    log("生成素材：两小时 MKV × 3（内封英语 + 中文 SRT，音轨与台词同节奏）")
    media = RUN / "media"
    videos = {
        "main": media / "Main Movie (2020)" / "Main Movie (2020).mkv",
        "cancel": media / "Cancel Movie (2021)" / "Cancel Movie (2021).mkv",
        "offsync": media / "Offsync Movie (2022)" / "Offsync Movie (2022).mkv",
    }
    cached = {"main": MEDIA_CACHE / "main.mkv", "offsync": MEDIA_CACHE / "offsync.mkv"}
    if not all(path.exists() for path in cached.values()):
        # 先生成不同步的那部，最后生成主片：work/eng.srt 留下的是主片的原字幕
        make_movie(cached["offsync"], offset=2.5)
        make_movie(cached["main"])
    for key, source in (("main", "main"), ("cancel", "main"), ("offsync", "offsync")):
        videos[key].parent.mkdir(parents=True)
        shutil.copyfile(cached[source], videos[key])
    # 扫描把 5 分钟内改过的文件当「疑似写入中」暂缓入账：素材是刚生成的，改到一小时前
    an_hour_ago = time.time() - 3600
    for video in videos.values():
        os.utime(video, (an_hour_ago, an_hour_ago))
    size = videos["main"].stat().st_size
    read_rate = size / READ_SECONDS
    log(
        f"    每部 {size / 1024 / 1024:.1f} MB，"
        f"存储限速 {read_rate / 1024 / 1024:.2f} MB/s（通读约 {READ_SECONDS} 秒）"
    )
    SCENARIO_STATE["videos"] = {k: str(v) for k, v in videos.items()}

    gateway_proc = subprocess.Popen(
        [PY, str(REPO / "scripts/perf/fake_llm_gateway.py"), "--port", str(GATEWAY_PORT)]
    )
    env = backend_env(read_rate)
    api = start_api(env, "api.log")
    SCENARIO_STATE["api"] = api
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{API}/auth/bootstrap", json={"username": "admin", "password": "e2e-pass-1"}
            )
            assert r.status_code == 200, r.text
            r = client.post(
                f"{API}/llm/providers",
                json={
                    "name": "假网关",
                    "provider_type": "openai_compat",
                    "base_url": f"{GATEWAY}/v1",
                    "api_key": "sk-fake",
                    "default_model": "fake-model",
                    "extra_models": [
                        {"id": "fake-model", "context_window": 128000, "max_output_tokens": 8000}
                    ],
                },
            )
            assert r.status_code == 200, r.text
            r = client.post(
                f"{API}/libraries",
                json={
                    "name": "端到端字幕",
                    "kind": "video",
                    "root_paths": [str(media)],
                },
            )
            assert r.status_code == 200, r.text
            library_id = r.json()["data"]["id"]
            deadline = time.monotonic() + 180
            files: dict[str, dict] = {}
            while time.monotonic() < deadline and len(files) < 3:
                time.sleep(1)
                items = client.get(f"{API}/libraries/{library_id}/items").json()["data"]
                for item in items:
                    detail = client.get(
                        f"{API}/libraries/{library_id}/items/{item['media_item_id']}"
                    ).json()["data"]
                    for f in detail["files"]:
                        if f.get("duration_seconds") and f.get("subtitle_streams"):
                            files[f["file_path"]] = f
            by_key = {k: files[str(v)] for k, v in videos.items() if str(v) in files}
            assert len(by_key) == 3, f"扫描没入账三部片子：{list(files)}"
            summary = ", ".join(f"{key}=#{row['id']}" for key, row in by_key.items())
            log(f"    媒体库扫描完成：{summary}")

            scenario_main(client, by_key["main"]["id"], videos["main"])
            api = scenario_bilingual_crash(client, by_key["main"]["id"], env)
            scenario_queue_cancel_offsync(
                client, by_key["cancel"]["id"], videos["cancel"], by_key["offsync"]["id"]
            )
            scenario_upstream_outage(client, by_key["main"]["id"])
    finally:
        for proc in (SCENARIO_STATE.get("api"), gateway_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=15)
        subprocess.run(["pkill", "-f", str(RUN)], capture_output=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n共 {len(CHECKS)} 项检查，通过 {len(CHECKS) - len(failed)} 项")
    if failed:
        print("未通过：" + "；".join(failed))
        sys.exit(1)
    print("SUBTITLE-E2E-OK")


if __name__ == "__main__":
    main()
