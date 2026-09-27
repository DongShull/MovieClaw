#!/usr/bin/env bash
# iOS 性能实验室：可复现、隔离的本地后端 + 数据 + 链路模拟。
#
# 给 iOS 模拟器测「媒体库首页」「订阅首页」打开速度用。模拟器与 Mac 共用网络栈，
# App 的服务器地址填 http://127.0.0.1:18601（LAN 链路）或 :18602（WAN 链路）。
#
#   端口（只用 18600–18609）
#     18600  后端（uvicorn，无热重载，只听 127.0.0.1）——压测直连口
#     18601  链路模拟 lan：RTT 6ms，下行 200 Mbps / 上行 50 Mbps（全连接共享）
#     18602  链路模拟 wan：RTT 70ms，下行 25 Mbps / 上行 8 Mbps（全连接共享）
#     18603  离线假图床（TMDB 图片 CDN 替身，同时是后端 image/tmdb 服务的出网代理）
#     18604  假 qBittorrent（订阅页「下载中」与任务中心的数据来源）
#   账号：admin / perf-lab-2026（超管），member / perf-lab-2026（成员，全部库可见、可订阅）
#   数据：$MC_LAB_DIR（默认 ~/workspace/.mc-perf-lab），仓库里不落任何运行期数据
#
# 用法：scripts/perf/ios_lab.sh <命令>
#   reset    清空实验室目录 → 迁移 → 建账号 → 灌全部数据（结束时进程全部停掉，缓存全冷）
#   start    起假图床 + 后端 + 两条链路模拟（后台运行，pid 与日志在实验室目录）
#   stop     只停本脚本起的进程（按 pid 文件，并核对命令行与工作目录）
#   status   各进程与端口状态、磁盘占用
#   warm     按 App 的请求形态把两个首页的全部图片请求一遍（预热服务端图片缓存）
#   bench    重启后端（保证冷）后串行压测两个首页的接口：首次 + 热态 P50/P95；
#            额外参数原样传给 ios_lab.py bench（如 --user member / --repeat 30 / --images），
#            加 --no-restart 则不重启
#   verify   经 18601 / 18602 用 curl 验收全部接口与各类图片
#   stats    数据集统计
#   logs     跟随后端访问日志
#
# 可调环境变量：MC_LAB_DIR、MC_LAB_PYTHON（默认仓库 .venv）、MC_LAB_TMDB_ENV（读 TMDB_API_KEY 的
# .env，默认仓库根目录的 .env）、MC_LAB_UPSTREAM_PROXY（TMDB 接口隧道的上游 HTTP 代理，如本机
# Surge/Clash 的 http://127.0.0.1:8888；默认直连）、MC_LAB_SCHEDULER（默认 false：
# 关掉定时任务，免得后台任务在测量时抢 CPU / 出网）、MC_LAB_ORIGIN_DELAY_MS 与
# MC_LAB_ORIGIN_MBPS（假图床的首字节延迟与限速，模拟 CDN，默认不加）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PERF="$REPO/scripts/perf"
LAB="${MC_LAB_DIR:-$HOME/workspace/.mc-perf-lab}"
PY="${MC_LAB_PYTHON:-$REPO/.venv/bin/python}"
TMDB_ENV_FILE="${MC_LAB_TMDB_ENV:-$REPO/.env}"
UPSTREAM_PROXY="${MC_LAB_UPSTREAM_PROXY:-}"
DATA="$LAB/data"
RUN="$LAB/run"
LOGS="$LAB/logs"
LSOF=/usr/sbin/lsof
BACKEND_PORT=18600 LAN_PORT=18601 WAN_PORT=18602 ORIGIN_PORT=18603 QBT_PORT=18604

say() { printf '\033[1;36m[ios-lab]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ios-lab] %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31m[ios-lab] %s\033[0m\n' "$*" >&2; exit 1; }

# 后端的全部路径与开关都经环境变量指到实验室目录（config.py 的 Settings 字段）
backend_env() {
  export PYTHONPATH="$REPO/src"
  export MOVIECLAW_DATA_DIR="$DATA"
  export DATABASE_URL="sqlite+aiosqlite:///$DATA/movieclaw.db"
  export METADATA_DIR="$DATA/metadata"
  export IMAGE_CACHE_DIR="$DATA/cache/images"
  export MEDIA_DIR="$DATA/uploads"
  export LOG_DIR="$DATA/logs"
  export SECRET_KEY_FILE="$DATA/.secret_key"
  export SITE_CONFIGS_DIR="$DATA/site-configs"
  export APP_HOST=127.0.0.1 APP_PORT=$BACKEND_PORT APP_RELOAD=false APP_ENV=perf-lab
  export APP_LOG_LEVEL=INFO APP_ACCESS_LOG_ENABLED=true
  # 图床基址改成 http：图片以正向代理的绝对 URL 到达假图床，不需要 CONNECT/TLS
  export TMDB_IMAGE_BASE_URL="http://image.tmdb.org/t/p"
  export SCHEDULER_ENABLED="${MC_LAB_SCHEDULER:-false}"
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
}

# —— 进程管理：pid 文件 + 核对（命令行特征 + 工作目录 = 实验室目录）——————————

pid_of() { cat "$RUN/$1.pid" 2>/dev/null || true; }

is_ours() {  # is_ours <名字> <命令行特征>
  local pid; pid="$(pid_of "$1")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= | grep -q -- "$2" || return 1
  [[ "$($LSOF -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" == "$LAB" ]]
}

pattern_of() {
  case "$1" in
    backend) echo "movieclaw_api.main" ;;
    origin) echo "fake_image_origin.py" ;;
    netem-lan) echo "netem_proxy.py --profile lan" ;;
    netem-wan) echo "netem_proxy.py --profile wan" ;;
  esac
}

port_busy() { [[ -n "$($LSOF -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true)" ]]; }

guard_port() {  # guard_port <端口> <名字>：端口被别人占着就拒绝启动
  if port_busy "$1" && ! is_ours "$2" "$(pattern_of "$2")"; then
    die "端口 $1 已被其它进程占用（$($LSOF -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" pid="$2}')），实验室不会去动它"
  fi
}

launch() {  # launch <名字> <日志> <命令...>：后台起进程、记 pid（工作目录 = 实验室目录）
  local name="$1" log="$2"; shift 2
  (
    cd "$LAB"
    # 单条命令直接放后台：$! 就是 nohup exec 成的目标进程本身（写成 cd && nohup … & 的话
    # 放到后台的是一个子 shell，pid 文件记的就不是它了）
    nohup "$@" >>"$log" 2>&1 &
    echo $! >"$RUN/$name.pid"
  )
}

wait_port() {  # wait_port <端口> <名字> <秒>
  local deadline=$((SECONDS + $3))
  until port_busy "$1"; do
    is_ours "$2" "$(pattern_of "$2")" || { tail -n 40 "$LOGS/$2.log" >&2 || true; die "$2 启动失败（见 $LOGS/$2.log）"; }
    ((SECONDS < deadline)) || die "$2 在 $3 秒内没有监听 $1"
    sleep 0.3
  done
}

start_origin() {
  is_ours origin "$(pattern_of origin)" && return
  guard_port $ORIGIN_PORT origin; guard_port $QBT_PORT origin
  if [[ -n "$UPSTREAM_PROXY" ]] && ! (exec 3<>"/dev/tcp/127.0.0.1/${UPSTREAM_PROXY##*:}") 2>/dev/null; then
    warn "上游代理 $UPSTREAM_PROXY 连不上：发现页的 TMDB 接口会失败（图片不受影响，全部离线生成）"
  fi
  launch origin "$LOGS/origin.log" "$PY" "$PERF/fake_image_origin.py" --pool-dir "$LAB/origin-pool" \
    --listen 127.0.0.1:$ORIGIN_PORT --upstream-proxy "$UPSTREAM_PROXY" \
    --delay-ms "${MC_LAB_ORIGIN_DELAY_MS:-0}" --rate-mbps "${MC_LAB_ORIGIN_MBPS:-0}" \
    --qbt-listen 127.0.0.1:$QBT_PORT --qbt-torrents "$LAB/origin/qbt-torrents.json"
  wait_port $ORIGIN_PORT origin 15
  say "假图床 127.0.0.1:$ORIGIN_PORT、假 qBittorrent 127.0.0.1:$QBT_PORT 已启动（pid $(pid_of origin)）"
}

start_backend() {
  is_ours backend "$(pattern_of backend)" && return
  guard_port $BACKEND_PORT backend
  (
    backend_env
    # TMDB 密钥只在后端进程的环境里出现：不回显、不写任何文件
    TMDB_API_KEY=""
    if [[ -f "$TMDB_ENV_FILE" ]]; then
      TMDB_API_KEY="$(sed -n 's/^TMDB_API_KEY=//p' "$TMDB_ENV_FILE" | head -n1 | tr -d "\r\"' ")"
    fi
    if [[ -z "$TMDB_API_KEY" ]]; then
      # 订阅接口构造服务时就要一个已配置的 TMDB 客户端（没配直接 500），但列表 / 预告 /
      # 刚刚入库都不真的打 TMDB：给一个占位 v3 密钥，订阅页照常可测，只有发现页不可用
      warn "没读到 $TMDB_ENV_FILE 里的 TMDB_API_KEY：用占位密钥启动（订阅页正常，发现页不可用）"
      TMDB_API_KEY=00000000000000000000000000000000
    fi
    export TMDB_API_KEY
    launch backend "$LOGS/backend.log" "$PY" -m movieclaw_api.main
  )
  local deadline=$((SECONDS + 90))
  until [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$BACKEND_PORT/api/v1/health" || true)" == 200 ]]; do
    is_ours backend "$(pattern_of backend)" || { tail -n 40 "$LOGS/backend.log" >&2 || true; die "后端启动失败（见 $LOGS/backend.log）"; }
    ((SECONDS < deadline)) || die "后端 90 秒内没有就绪"
    sleep 0.5
  done
  say "后端 http://127.0.0.1:$BACKEND_PORT 已就绪（pid $(pid_of backend)）"
}

start_netem() {  # start_netem <lan|wan>
  local name="netem-$1" port; port=$([[ $1 == lan ]] && echo $LAN_PORT || echo $WAN_PORT)
  is_ours "$name" "$(pattern_of "$name")" && return
  guard_port "$port" "$name"
  launch "$name" "$LOGS/$name.log" "$PY" "$PERF/netem_proxy.py" --profile "$1" \
    --upstream 127.0.0.1:$BACKEND_PORT
  wait_port "$port" "$name" 10
  say "链路模拟 $1 127.0.0.1:$port → 后端（pid $(pid_of "$name")）"
}

stop_one() {  # stop_one <名字>
  local name="$1" pid; pid="$(pid_of "$name")"
  [[ -n "$pid" ]] || return 0
  if is_ours "$name" "$(pattern_of "$name")"; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    say "已停止 $name（pid $pid）"
  else
    warn "$name 的 pid 文件（$pid）对应的不是实验室进程，已忽略（不杀）"
  fi
  rm -f "$RUN/$name.pid"
}

access_log() { echo "$DATA/logs/movieclaw-$(date +%F).log"; }

# —— 命令 ——————————————————————————————————————————————————————————————

cmd_stop() {
  mkdir -p "$RUN"
  for name in netem-wan netem-lan backend origin; do stop_one "$name"; done
}

cmd_start() {
  [[ -f "$DATA/movieclaw.db" ]] || die "实验室还没有数据，先运行：$0 reset"
  mkdir -p "$RUN" "$LOGS"
  start_origin
  start_backend
  start_netem lan
  start_netem wan
  cat <<EOF

  App 服务器地址：http://127.0.0.1:$LAN_PORT（lan）  http://127.0.0.1:$WAN_PORT（wan）
  账号：admin / perf-lab-2026    member / perf-lab-2026
  直连压测口：http://127.0.0.1:$BACKEND_PORT
  访问日志：$(access_log)
           （行格式：时间 | INFO | movieclaw_api.access | method=GET path=/api/v1/... status_code=200 duration_ms=12.34 message=request completed）
  进程日志：$LOGS/{backend,origin,netem-lan,netem-wan}.log
EOF
}

cmd_status() {
  for name in backend origin netem-lan netem-wan; do
    if is_ours "$name" "$(pattern_of "$name")"; then
      printf '  %-10s 运行中  pid %s\n' "$name" "$(pid_of "$name")"
    else
      printf '  %-10s 未运行\n' "$name"
    fi
  done
  for port in $BACKEND_PORT $LAN_PORT $WAN_PORT $ORIGIN_PORT $QBT_PORT; do
    printf '  端口 %s：%s\n' "$port" "$(port_busy "$port" && echo 监听中 || echo 空闲)"
  done
  if [[ -d "$LAB" ]]; then
    printf '  实验室目录 %s：%s（数据库 %s，图片缓存 %s，底图池 %s）\n' "$LAB" "$(du -sh "$LAB" | cut -f1)" \
      "$(du -sh "$DATA/movieclaw.db" 2>/dev/null | cut -f1)" "$(du -sh "$DATA/cache/images" 2>/dev/null | cut -f1 || echo 0)" \
      "$(du -sh "$LAB/origin-pool" 2>/dev/null | cut -f1)"
    printf '  访问日志：%s\n' "$(access_log)"
  fi
}

cmd_reset() {
  local started=$SECONDS
  [[ -x "$PY" ]] || die "找不到 Python：$PY"
  if [[ -d "$RUN" ]]; then cmd_stop; fi
  for port in $BACKEND_PORT $LAN_PORT $WAN_PORT $ORIGIN_PORT $QBT_PORT; do
    port_busy "$port" && die "端口 $port 已被其它进程占用，实验室不会去动它"
  done
  [[ "$LAB" == */.mc-perf-lab* ]] || die "拒绝清空非实验室目录：$LAB"
  trap 'cmd_stop' EXIT  # 中途失败也要把临时起的后端 / 假图床停掉（stop 是幂等的）
  say "清空 $LAB"
  rm -rf "$LAB"
  mkdir -p "$DATA" "$RUN" "$LOGS" "$LAB/origin" "$LAB/bench"

  say "1/9 迁移：alembic upgrade head（DATABASE_URL=$DATA/movieclaw.db）"
  (backend_env && cd "$REPO" && "$PY" -m alembic -c alembic.ini upgrade head >"$LOGS/alembic.log" 2>&1) \
    || { tail -n 20 "$LOGS/alembic.log"; die "迁移失败"; }
  say "2/9 关闭 Jellyfin 兼容层（不去抢 UDP 7359）"
  (backend_env && cd "$LAB" && "$PY" "$PERF/ios_lab.py" prepare-settings)
  say "3/9 媒体库：seed_library_dataset.py --profile home"
  "$PY" -W ignore::DeprecationWarning "$PERF/seed_library_dataset.py" --db "$DATA/movieclaw.db" \
    --profile home | tail -n 16
  say "4/9 订阅：seed_subscriptions_dataset.py"
  (backend_env && "$PY" "$PERF/seed_subscriptions_dataset.py" --db "$DATA/movieclaw.db" \
    --qbt-json "$LAB/origin/qbt-torrents.json")
  say "5/9 本地海报：seed_poster_assets.py"
  "$PY" "$PERF/seed_poster_assets.py" --db "$DATA/movieclaw.db" --assets "$DATA/metadata/images"
  say "6/9 假图床底图池"
  "$PY" "$PERF/fake_image_origin.py" --pool-dir "$LAB/origin-pool" --build-pool
  say "7/9 临时启动后端，经 API 初始化账号与配置"
  start_origin
  start_backend
  "$PY" "$PERF/ios_lab.py" setup
  say "8/9 观看数据：seed_watch_state.py"
  (backend_env && "$PY" "$PERF/seed_watch_state.py" --db "$DATA/movieclaw.db" \
    --metadata-dir "$DATA/metadata" --pool-dir "$LAB/origin-pool" --member member)
  say "9/9 数据集统计"
  "$PY" "$PERF/ios_lab.py" stats
  cmd_stop
  say "reset 完成（$((SECONDS - started)) 秒）。进程已全部停止、缓存全冷；接着运行：$0 start"
}

cmd_bench() {
  local restart=1 args=()
  for arg in "$@"; do
    if [[ "$arg" == --no-restart ]]; then restart=0; else args+=("$arg"); fi
  done
  if ((restart)); then
    say "重启后端，保证第一轮是冷进程（SQLite 连接、进程内缓存全空；OS 页缓存无法清）"
    stop_one backend
    start_backend
    sleep 5  # 启动后的硬件探测等后台预热跑完，别算进首次请求
  fi
  "$PY" "$PERF/ios_lab.py" bench ${args[@]+"${args[@]}"}  # 兼容 macOS 自带 bash 3.2 的 set -u
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    reset) cmd_reset ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    warm) "$PY" "$PERF/ios_lab.py" warm "$@" ;;
    bench) cmd_bench "$@" ;;
    verify) "$PY" "$PERF/ios_lab.py" verify "$@" ;;
    stats) "$PY" "$PERF/ios_lab.py" stats "$@" ;;
    logs) tail -F "$(access_log)" | grep --line-buffered 'movieclaw_api.access' ;;
    *) sed -n '2,32p' "$0"; exit 1 ;;
  esac
}

main "$@"
