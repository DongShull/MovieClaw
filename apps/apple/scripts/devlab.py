#!/usr/bin/env python3
"""真机实验台（播放体验迭代，docs/design/playback-qoe.md §2、§10）：调试版 App + devicectl 批量起播、按脚本跳转，
结果以 `-mcLab devlab-<批次>:<标签>` 写进服务器的播放记录，report 从 NAS 只读汇总。用法（先设 MC_DEVICE）：
  devlab.py run <批次> <片名...|all> [-- 额外启动参数...]
  devlab.py ab <批次> <轮数> <实验名>
  devlab.py report <批次>          从 NAS 记录按组/按片汇总"""
import json, os, re, statistics, subprocess, sys, time
from collections import defaultdict
HERE = os.path.expanduser(os.environ.get("MC_LAB_DIR", "~/workspace/.mc-lab"))   # 日志与批次结果（不入库）
LONG = "14:+10,22:+600,32:-5,40:300,48:+20"
SHORT = "14:+10,22:+120,32:-5,40:60,48:+20"
DEVICE = os.environ.get("MC_DEVICE", "")   # 真机 UDID：xcrun devicectl list devices
# report 经 SSH 在 NAS 的 movieclaw 容器里只读查播放记录（MC_NAS_SSH，默认 root@192.168.1.10）
CORPUS_FILE = os.path.expanduser(os.environ.get("MC_LAB_CORPUS", "~/.config/movieclaw/devlab-corpus.json"))
# 语料是自己片库里的条目（路由 /play/<条目>[/sXXeYY]?file=<文件>&t=<秒>），属于个人数据，不入库：
# {"corpus": {"片名": "/play/…" 或 {"route": "/play/…", "plan": "秒:+跳,…"}},
#  "experiments": {"实验名": {"arms": {"组名": [启动参数…]}, "titles": [片名…]}}}
_cfg = json.load(open(CORPUS_FILE, encoding="utf-8")) if os.path.exists(CORPUS_FILE) else {"corpus": {}, "experiments": {}}
CORPUS = {k: (v if isinstance(v, str) else (v["route"], v["plan"])) for k, v in _cfg["corpus"].items()}
EXPERIMENTS = {k: (v["arms"], v["titles"]) for k, v in _cfg["experiments"].items()}

def entry(name):
    v = CORPUS[name]; return (v, LONG) if isinstance(v, str) else v

def run_one(batch, name, extra=(), tag=None, seconds=66, close_at=56):
    route, plan = entry(name); tag = tag or name
    os.makedirs(f"{HERE}/dl-{batch}", exist_ok=True)
    path = f"{HERE}/dl-{batch}/{tag}.log"
    args = ["xcrun", "devicectl", "device", "process", "launch", "--console", "--terminate-existing",
            "--device", DEVICE, "io.movieclaw.app", "--", "-mcAetherLog", "YES", "-mcNoProgress", "YES",
            "-mcNoServerFallback", "YES", "-mcFrameStatsEverySecond", "YES", "-mcRoute", route, "-mcRouteDelay", "2",
            "-mcAutoSeek", plan, "-mcLab", f"devlab-{batch}:{tag}", "-mcAutoCloseAfter", str(close_at), *extra]
    with open(path, "w") as log:
        p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT); time.sleep(seconds); p.kill(); p.wait()
    t = open(path, encoding="utf-8", errors="replace").read()
    ff = re.search(r"\[StartupTrace\].*首帧 (\d+)", t)
    seeks = re.findall(r"\[SeekTrace\] \d+ → \d+ 秒（(缓冲内|缓冲外)[^）]*）耗时 (\d+) 毫秒", t)
    snaps = len(re.findall(r"P36\] seek snapped", t))
    err = re.findall(r"\[PlayerError\] (.*)", t)
    s = " ".join(("内" if k == "缓冲内" else "外") + ms for k, ms in seeks)
    print(f"{tag:34} 首帧 {ff.group(1) if ff else None} 跳转[{s}] 吸附{snaps} {'报错:'+err[0][:60] if err else ''}", flush=True)

def nas(query):
    return json.loads(subprocess.run(["ssh", "-o", "BatchMode=yes", os.environ.get("MC_NAS_SSH", "root@192.168.1.10"),
        "/usr/local/bin/docker exec -i movieclaw python3 -"], input=query, capture_output=True, text=True, check=True).stdout)

def report(batch):
    rows = nas(r'''
import sqlite3, json, sys
c = sqlite3.connect("file:/app/data/movieclaw.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
out = []
for r in c.execute("select lab_scenario, first_frame_ms, undisturbed, rebuffer_count, rebuffer_ms, detail from playback_metric where lab_scenario like ? order by id", ("devlab-%s:%%",)):
    d = json.loads(r["detail"] or "{}")
    out.append({"tag": r["lab_scenario"].split(":", 1)[1], "ff": r["first_frame_ms"], "ok": r["undisturbed"], "st": r["rebuffer_count"],
                "stms": r["rebuffer_ms"], "why": (d.get("judgement") or {}).get("reasons"), "seeks": d.get("seeks", [])})
print(json.dumps(out, ensure_ascii=False))
''' % batch)
    def p(v, q):
        s = sorted(v); return s[min(len(s) - 1, round(q * (len(s) - 1)))] if s else None
    arms = defaultdict(lambda: {"n": 0, "ok": 0, "ff": [], "in": [], "out": [], "st": 0})
    for r in rows:
        parts = r["tag"].split("@"); a = parts[1] if len(parts) > 1 else "-"
        x = arms[a]; x["n"] += 1; x["ok"] += r["ok"] or 0; x["st"] += r["st"] or 0
        if r["ff"]: x["ff"].append(r["ff"])
        for s in r["seeks"]:
            if s.get("ms") is not None: (x["in"] if s.get("buffered") else x["out"]).append(s["ms"])
        if len(parts) == 1:
            print(f"  {r['tag']:32} 首帧 {r['ff']} 卡顿 {r['st']}/{r['stms']}ms 无打扰 {r['ok']} {r['why'] or ''} "
                  + " ".join(("内" if s.get('buffered') else "外") + str(s.get('ms')) for s in r["seeks"]))
    for a, x in sorted(arms.items()):
        print(f"{a}: {x['ok']}/{x['n']} 无打扰  首帧 {p(x['ff'],.5)}/{p(x['ff'],.9)}  缓冲内 {p(x['in'],.5)}/{p(x['in'],.9)}  缓冲外 {p(x['out'],.5)}/{p(x['out'],.9)}  卡顿 {x['st']}")

def main():
    if not DEVICE and sys.argv[1] != "report": sys.exit("先设 MC_DEVICE=<真机 UDID>")
    cmd = sys.argv[1]
    if cmd == "run":
        batch, rest = sys.argv[2], sys.argv[3:]
        extra = rest[rest.index("--") + 1:] if "--" in rest else []
        names = rest[:rest.index("--")] if "--" in rest else rest
        for n in (list(CORPUS) if names == ["all"] else names): run_one(batch, n, extra)
    elif cmd == "ab":
        batch, rounds, exp = sys.argv[2], int(sys.argv[3]), sys.argv[4]
        arms, titles = EXPERIMENTS[exp]; keys = list(arms)
        for r in range(rounds):
            for i, t in enumerate(titles):
                k = (r + i) % len(keys)
                for a in keys[k:] + keys[:k]: run_one(batch, t, arms[a], tag=f"{t}@{a}@r{r}"); time.sleep(2)
    elif cmd == "report":
        report(sys.argv[2])
if __name__ == "__main__": main()
