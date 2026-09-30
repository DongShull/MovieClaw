#!/usr/bin/env python3
"""真机实验台（播放体验迭代，docs/design/playback-qoe.md §2、§10）：调试版 App + devicectl 批量起播、按脚本跳转，
结果以 `-mcLab devlab-<批次>:<标签>` 写进服务器的播放记录，report 从 NAS 只读汇总。用法（先设 MC_DEVICE）：
  devlab.py run <批次> <片名...|all> [-- 额外启动参数...]
  devlab.py ab <批次> <轮数> <实验名>
  devlab.py start <批次> <起播秒|keep> <轮数> <片名...|all> [-- 额外启动参数...]
                                   只量起播：不跳转、18 秒一部；起播秒覆盖路由里的 t=（keep 保留原值）
  devlab.py startab <批次> <起播秒|keep> <轮数> <实验名>   只量起播的交替对照
  devlab.py relaunchab <批次> <起播秒|keep> <轮数> <实验名> 每组先热身一遍再冷启动量（片源缓存跨启动）
  devlab.py reopen <批次> <起播秒|keep> <轮数> <片名...>    同一进程里 9 秒后原样重开（「全热」的上限）
  devlab.py timeline <日志目录或文件...>   本机日志拆起播时间线（各段距「装载」的毫秒）
  devlab.py compare <日志目录> [段...]     交替对照汇总：各组首帧 / 开播中位与 p90、按片配对差值
  devlab.py report <批次>          从 NAS 记录按组/按片汇总"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = os.path.expanduser(
    os.environ.get("MC_LAB_DIR", "~/workspace/.mc-lab")
)  # 日志与批次结果（不入库）
LONG = "14:+10,22:+600,32:-5,40:300,48:+20"
SHORT = "14:+10,22:+120,32:-5,40:60,48:+20"
DEVICE = os.environ.get("MC_DEVICE", "")  # 真机 UDID：xcrun devicectl list devices
# App 启动后多久打开播放器（秒）。要让启动后的空闲预热先跑完时调大（MC_ROUTE_DELAY=6），每部片的时长跟着加
ROUTE_DELAY = float(os.environ.get("MC_ROUTE_DELAY", "2"))
ROUTE_FIRST = os.environ.get("MC_ROUTE_FIRST", "")
ROUTE_THEN_DELAY = float(os.environ.get("MC_ROUTE_THEN_DELAY", "3"))
# report 经 SSH 在 NAS 的 movieclaw 容器里只读查播放记录（MC_NAS_SSH，默认 root@192.168.1.10）
CORPUS_FILE = os.path.expanduser(
    os.environ.get("MC_LAB_CORPUS", "~/.config/movieclaw/devlab-corpus.json")
)
# 语料是自己片库里的条目（路由 /play/<条目>[/sXXeYY]?file=<文件>&t=<秒>），属于个人数据，不入库：
# {"corpus": {"片名": "/play/…" 或 {"route": "/play/…", "plan": "秒:+跳,…"}},
#  "experiments": {"实验名": {"arms": {"组名": [启动参数…]}, "titles": [片名…]}}}
_cfg = (
    json.loads(Path(CORPUS_FILE).read_text(encoding="utf-8"))
    if os.path.exists(CORPUS_FILE)
    else {"corpus": {}, "experiments": {}}
)
CORPUS = {
    k: (v if isinstance(v, str) else (v["route"], v["plan"])) for k, v in _cfg["corpus"].items()
}
EXPERIMENTS = {k: (v["arms"], v["titles"]) for k, v in _cfg["experiments"].items()}


def entry(name):
    v = CORPUS[name]
    return (v, LONG) if isinstance(v, str) else v


def with_start(route, at):
    """把路由里的 t= 换成 at（keep 不动；0 表示从头播）"""
    if at == "keep":
        return route
    route = re.sub(r"([?&])t=[^&]*&?", r"\1", route).rstrip("?&")
    return route + ("&" if "?" in route else "?") + f"t={at}"


def run_one(batch, name, extra=(), tag=None, seconds=66, close_at=56, at="keep", seek=True):
    route, plan = entry(name)
    route = with_start(route, at)
    tag = tag or name
    os.makedirs(f"{HERE}/dl-{batch}", exist_ok=True)
    path = f"{HERE}/dl-{batch}/{tag}.log"
    args = [
        "xcrun",
        "devicectl",
        "device",
        "process",
        "launch",
        "--console",
        "--terminate-existing",
        "--device",
        DEVICE,
        "io.movieclaw.app",
        "--",
        "-mcAetherLog",
        "YES",
        "-mcNoProgress",
        "YES",
        "-mcNoServerFallback",
        "YES",
        "-mcFrameStatsEverySecond",
        "YES",
        # MC_ROUTE_FIRST=/library：先开这个页面，停 MC_ROUTE_THEN_DELAY 秒（默认 3）再开播放页（量页面上的预连）
        *(
            [
                "-mcRoute",
                ROUTE_FIRST,
                "-mcRouteThen",
                route,
                "-mcRouteThenDelay",
                str(ROUTE_THEN_DELAY),
            ]
            if ROUTE_FIRST
            else ["-mcRoute", route]
        ),
        "-mcRouteDelay",
        str(ROUTE_DELAY),
        *(["-mcAutoSeek", plan] if seek else []),
        "-mcLab",
        f"devlab-{batch}:{tag}",
        *(["-mcAutoCloseAfter", str(close_at)] if close_at else []),
        *extra,
    ]
    for _ in range(2):
        with open(path, "w") as log:
            p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            time.sleep(
                seconds + max(0.0, ROUTE_DELAY - 2) + (ROUTE_THEN_DELAY if ROUTE_FIRST else 0)
            )
            # 先 SIGINT 让 devicectl 正常断开控制台连接，6 秒没退再强杀：直接 SIGKILL 会在设备上留下没断开的控制台，
            # 接下来带 --console 的启动一阵子都报 CoreDeviceError 10002「Invalid argument」（日志为空）。等它完全退出，
            # 免得它转发给 App 的中断信号晚到、落在下一次启动的实例上（出现过一次「App terminated due to signal 2」）
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        t = Path(path).read_text(encoding="utf-8", errors="replace")
        if "[NativeEngine" in t or "[PlayerError]" in t:
            break
        # devicectl 偶尔没接上 App 的输出（日志为空）：不是播放问题，重跑一次
        print(f"{tag:34} 日志为空，重跑", flush=True)
        time.sleep(3)
    ff = re.search(r"\[StartupTrace\].*首帧 (\d+)", t)
    seeks = re.findall(r"\[SeekTrace\] \d+ → \d+ 秒（(缓冲内|缓冲外)[^）]*）耗时 (\d+) 毫秒", t)
    snaps = len(re.findall(r"P36\] seek snapped", t))
    err = re.findall(r"\[PlayerError\] (.*)", t)
    s = " ".join(("内" if k == "缓冲内" else "外") + ms for k, ms in seeks)
    print(
        f"{tag:34} 首帧 {ff.group(1) if ff else None} 跳转[{s}] 吸附{snaps} {'报错:' + err[0][:60] if err else ''}",
        flush=True,
    )


def nas(query):
    return json.loads(
        subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                os.environ.get("MC_NAS_SSH", "root@192.168.1.10"),
                "/usr/local/bin/docker exec -i movieclaw python3 -",
            ],
            input=query,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def report(batch):
    rows = nas(
        r"""
import sqlite3, json, sys
c = sqlite3.connect("file:/app/data/movieclaw.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
out = []
for r in c.execute("select lab_scenario, first_frame_ms, undisturbed, rebuffer_count, rebuffer_ms, detail from playback_metric where lab_scenario like ? order by id", ("devlab-__BATCH__:%",)):
    d = json.loads(r["detail"] or "{}")
    out.append({"tag": r["lab_scenario"].split(":", 1)[1], "ff": r["first_frame_ms"], "ok": r["undisturbed"], "st": r["rebuffer_count"],
                "stms": r["rebuffer_ms"], "why": (d.get("judgement") or {}).get("reasons"), "seeks": d.get("seeks", [])})
print(json.dumps(out, ensure_ascii=False))
""".replace("__BATCH__", batch)
    )

    def p(v, q):
        s = sorted(v)
        return s[min(len(s) - 1, round(q * (len(s) - 1)))] if s else None

    arms = defaultdict(lambda: {"n": 0, "ok": 0, "ff": [], "in": [], "out": [], "st": 0})
    for r in rows:
        parts = r["tag"].split("@")
        a = parts[1] if len(parts) > 1 else "-"
        x = arms[a]
        x["n"] += 1
        x["ok"] += r["ok"] or 0
        x["st"] += r["st"] or 0
        if r["ff"]:
            x["ff"].append(r["ff"])
        for s in r["seeks"]:
            if s.get("ms") is not None:
                (x["in"] if s.get("buffered") else x["out"]).append(s["ms"])
        if len(parts) == 1:
            print(
                f"  {r['tag']:32} 首帧 {r['ff']} 卡顿 {r['st']}/{r['stms']}ms 无打扰 {r['ok']} {r['why'] or ''} "
                + " ".join(
                    ("内" if s.get("buffered") else "外") + str(s.get("ms")) for s in r["seeks"]
                )
            )
    for a, x in sorted(arms.items()):
        print(
            f"{a}: {x['ok']}/{x['n']} 无打扰  首帧 {p(x['ff'], 0.5)}/{p(x['ff'], 0.9)}  缓冲内 {p(x['in'], 0.5)}/{p(x['in'], 0.9)}  缓冲外 {p(x['out'], 0.5)}/{p(x['out'], 0.9)}  卡顿 {x['st']}"
        )


# 起播时间线：各段在引擎日志里的标志（第一次出现为准），时间都按距「装载」算
TIMELINE = [
    ("load", r"\[NativeEngine [\d.]+\] 装载"),
    ("src", r"startup 1/8 sourceOpened"),
    ("cont", r"startup 2/8 containerOpened"),
    ("probe", r"startup 3/8 streamsProbed"),
    ("route", r"startup 5/8 routed"),
    ("plan", r"segment plan:"),
    ("sess", r"startup 6/8 sessionConstructed"),
    ("init", r"GET /\w+/init\.mp4"),
    ("segreq", r"GET /\w+/seg\d+\.mp4"),
    ("served", r"seg\d+: served"),
    ("ready", r"layer\.isReadyForDisplay=true"),
    ("pres", r"startup 8/8 presenting"),
    ("play", r"timeControlStatus=playing"),
]
STAMP = re.compile(r"^\[(?:Aether|NativeEngine) (\d+\.\d+)\]")


def timeline(path):
    """一份日志 → ({段: 距装载毫秒}, 起播分段那一行)"""
    got, last, trace = {}, None, None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = STAMP.match(line)
        if m:
            last = float(m.group(1))
        if trace is None and "[StartupTrace]" in line:
            trace = line
        for name, pat in TIMELINE:
            if name not in got and last is not None and re.search(pat, line):
                got[name] = last
    if "load" not in got:
        return None, trace
    return {k: int((v - got["load"]) * 1000) for k, v in got.items()}, trace


def trace_ms(trace, key):
    m = re.search(rf"{key} (\d+)", trace or "")
    return int(m.group(1)) if m else None


def log_files(args):
    for a in args:
        p = Path(a)
        yield from (sorted(p.glob("*.log")) if p.is_dir() else [p])


def print_timeline(args):
    cols = [n for n, _ in TIMELINE if n != "load"]
    print(f"{'片源':34}" + "".join(f"{c:>7}" for c in cols) + "   首帧 / 开播（点击起算）")
    for f in log_files(args):
        d, trace = timeline(f)
        if d is None:
            print(f"{f.stem[:34]:34} （没有装载）")
            continue
        tail = f"{trace_ms(trace, '首帧')} / {trace_ms(trace, '播放')}"
        print(f"{f.stem[:34]:34}" + "".join(f"{d.get(c, ''):>7}" for c in cols) + "   " + tail)


def compare(root, stages):
    """标签「片名@组@r轮」的日志按组汇总；「@r0w」这类热身不计"""

    def pct(v, q):
        s = sorted(v)
        return s[min(len(s) - 1, round(q * (len(s) - 1)))] if s else None

    arms = defaultdict(lambda: defaultdict(list))
    titles = defaultdict(lambda: defaultdict(list))
    for f in log_files([root]):
        parts = f.stem.split("@")
        if len(parts) < 3 or parts[2].endswith("w"):
            continue
        d, trace = timeline(f)
        ff = trace_ms(trace, "首帧")
        if ff is None:
            continue
        x = arms[parts[1]]
        x["首帧"].append(ff)
        for key in ("播放", "引擎"):
            if trace_ms(trace, key) is not None:
                x[key].append(trace_ms(trace, key))
        for s in stages:
            if d and s in d:
                x[s].append(d[s])
        titles[parts[0]][parts[1]].append(ff)
    names = sorted(arms)
    for a in names:
        x = arms[a]
        cells = [
            f"首帧 {pct(x['首帧'], 0.5)}/{pct(x['首帧'], 0.9)}",
            f"开播 {pct(x['播放'], 0.5)}/{pct(x['播放'], 0.9)}",
            f"引擎 {pct(x['引擎'], 0.5)}",
        ] + [f"{s} {pct(x[s], 0.5)}" for s in stages]
        print(f"{a}: " + "  ".join(cells) + f"  （{len(x['首帧'])} 条）")
    if len(names) == 2:
        a, b = names
        diffs = []
        for t, v in sorted(titles.items()):
            if v.get(a) and v.get(b):
                ma, mb = sorted(v[a])[len(v[a]) // 2], sorted(v[b])[len(v[b]) // 2]
                diffs.append(mb - ma)
                print(f"  {t[:30]:30} {ma:>6} → {mb:>6}  ({mb - ma:+d})")
        if diffs:
            diffs.sort()
            faster = sum(1 for x in diffs if x < 0)
            print(f"  配对差值中位 {diffs[len(diffs) // 2]:+d} 毫秒，{faster}/{len(diffs)} 部变快")


def main():
    if not DEVICE and sys.argv[1] not in ("report", "timeline", "compare"):
        sys.exit("先设 MC_DEVICE=<真机 UDID>")
    cmd = sys.argv[1]
    if cmd == "run":
        batch, rest = sys.argv[2], sys.argv[3:]
        extra = rest[rest.index("--") + 1 :] if "--" in rest else []
        names = rest[: rest.index("--")] if "--" in rest else rest
        for n in list(CORPUS) if names == ["all"] else names:
            run_one(batch, n, extra)
    elif cmd == "ab":
        batch, rounds, exp = sys.argv[2], int(sys.argv[3]), sys.argv[4]
        arms, titles = EXPERIMENTS[exp]
        keys = list(arms)
        for r in range(rounds):
            for i, t in enumerate(titles):
                k = (r + i) % len(keys)
                for a in keys[k:] + keys[:k]:
                    run_one(batch, t, arms[a], tag=f"{t}@{a}@r{r}")
                    time.sleep(2)
    elif cmd in ("start", "startab"):
        batch, at, rounds = sys.argv[2], sys.argv[3], int(sys.argv[4])
        rest = sys.argv[5:]
        extra = rest[rest.index("--") + 1 :] if "--" in rest else []
        rest = rest[: rest.index("--")] if "--" in rest else rest
        if cmd == "start":
            arms, titles = {"-": extra}, (list(CORPUS) if rest == ["all"] else rest)
        else:
            arms, titles = EXPERIMENTS[rest[0]]
        keys = list(arms)
        for r in range(rounds):
            for i, t in enumerate(titles):
                k = (r + i) % len(keys)
                for a in keys[k:] + keys[:k]:
                    tag = f"{t}@{a}@r{r}" if cmd == "startab" or rounds > 1 else t
                    run_one(
                        batch,
                        t,
                        arms[a] + (extra if cmd == "startab" else []),
                        tag=tag,
                        seconds=18,
                        close_at=12,
                        at=at,
                        seek=False,
                    )
                    time.sleep(2)
    elif cmd == "relaunchab":
        # 续播跨启动（引擎补丁 P42）的交替对照：每部片每组先热身一遍（把片源字节缓存填上、App 随后被杀），
        # 再冷启动量一遍。两组都热身，NAS 的页缓存对两组一样
        batch, at, rounds, exp = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
        arms, titles = EXPERIMENTS[exp]
        keys = list(arms)
        for r in range(rounds):
            for i, t in enumerate(titles):
                k = (r + i) % len(keys)
                for a in keys[k:] + keys[:k]:
                    run_one(
                        batch,
                        t,
                        # MC_PURGE_WARMUP=1：热身前清空跨启动缓存，每组都从零开始填（比较缓存记法本身时用，免得两组互相沾光）
                        arms[a]
                        + (
                            ["-mcPurgeByteCache", "YES"]
                            if os.environ.get("MC_PURGE_WARMUP") == "1"
                            else []
                        ),
                        tag=f"{t}@{a}@r{r}w",
                        seconds=18,
                        close_at=12,
                        at=at,
                        seek=False,
                    )
                    time.sleep(2)
                    run_one(
                        batch,
                        t,
                        arms[a],
                        tag=f"{t}@{a}@r{r}",
                        seconds=18,
                        close_at=12,
                        at=at,
                        seek=False,
                    )
                    time.sleep(2)
    elif cmd == "reopen":
        # 同一次 App 运行里开两次同一个起播点：第二次片源字节缓存（P22）已有文件头、索引与起播点附近的数据，
        # 两次的差就是「续播时数据都在本机」能省下的上限
        batch, at, rounds = sys.argv[2], sys.argv[3], int(sys.argv[4])
        rest = sys.argv[5:]
        extra = rest[rest.index("--") + 1 :] if "--" in rest else []
        titles = rest[: rest.index("--")] if "--" in rest else rest
        for r in range(rounds):
            for t in titles:
                run_one(
                    batch,
                    t,
                    ["-mcRouteReopenAfter", "9", *extra],
                    tag=f"{t}@-@r{r}",
                    seconds=26,
                    close_at=0,
                    at=at,
                    seek=False,
                )
                time.sleep(2)
    elif cmd == "timeline":
        print_timeline(sys.argv[2:])
    elif cmd == "compare":
        compare(sys.argv[2], sys.argv[3:] or ["src", "cont", "served"])
    elif cmd == "report":
        report(sys.argv[2])


if __name__ == "__main__":
    main()
