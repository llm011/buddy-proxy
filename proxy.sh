#!/usr/bin/env bash
# proxy.sh - manage the buddy-proxy (buddy_proxy) FastAPI server
#
# Usage:
#   ./proxy.sh start    [-p PORT] [-H HOST]     # 后台启动 (默认 0.0.0.0:8787，局域网可访问)
#   ./proxy.sh stop     [-p PORT] [-H HOST]     # 停止（非默认端口/host 需带同样参数）
#   ./proxy.sh restart  [-p PORT] [-H HOST]     # 重启
#   ./proxy.sh status                            # 查看状态
#   ./proxy.sh logs                              # 跟踪日志
#   ./proxy.sh ui                                # 确保在跑并打开管理页 http://127.0.0.1:8787/ui
#   ./proxy.sh login    [provider] [--no-browser] [--region cn|global] # 登录上游账号（默认 codebuddy；
#                                                # provider: codebuddy(=workbuddy, =codebuddyintl 海外)/trae(=traeintl 海外)/zcode/glm/doubao/mimo/qoder(=qoderintl 海外)/gemini/antigravity）
#                                                # --region 仅 trae/qoder/codebuddy 生效（两区账号不通用；交互终端不给会问一句）
#                                                # 登录成功后若网关在运行，需 restart 生效
#
# 支持环境变量（start 命令生效）：
#   PROXY_PORT, PROXY_HOST, PROXY_EXTRA_ARGS (附加传给 python -m buddy_proxy)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 日志/状态目录：统一放项目根 logs/（应用层 logger 按天滚动）
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# 默认配置
# HOST 默认 0.0.0.0：允许局域网内其它机器访问（如 192.168.31.100:8787）。
# 仅本机使用可显式指定：./proxy.sh start -H 127.0.0.1
PROXY_HOST="${PROXY_HOST:-0.0.0.0}"
PROXY_PORT="${PROXY_PORT:-8787}"
# 默认启用 ZCode（glm-* 编码通道，必须最先注册以免被 trae 截走）+ Trae（兜底通道）
# + 豆包（CDP 直连）+ MiMo（小米桌面登录态）+ Qoder（千问/GLM/Kimi 等）+
# Antigravity（Google 免费额度，Gemini 3.x / Claude / GPT-OSS）；
# 可用 PROXY_EXTRA_ARGS 覆盖，或命令行追加参数（如 --default-provider codebuddy）
EXTRA_ARGS="${PROXY_EXTRA_ARGS:---desensitize --trae --doubao --dumate --zcode --glm --zcode-start --mimo --qoder --antigravity --kimi --codex --default-provider trae}"

# 优先使用项目自带 .venv（uv 已装好依赖），否则退回系统 python
if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
    PYTHON_BIN="uv run python"
else
    PYTHON_BIN="python3"
fi

# 状态/日志文件（均在 logs/ 下）
PID_FILE="$LOG_DIR/proxy.pid"
LOG_FILE="$LOG_DIR/proxy.sh.log"

log() { printf '[proxy.sh] %s\n' "$*"; }

read_pid() {
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE"
    else
        echo ""
    fi
}

is_running() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# 端口实况是唯一事实：pid 文件可能指向已死/僵死/被复用的进程
find_port_pid() {
    lsof -ti tcp:"$PROXY_PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

# 防 PID 复用误判：确认该 pid 确实是我们的 buddy_proxy 进程
is_proxy_cmd() {
    ps -o command= -p "$1" 2>/dev/null | grep -q "buddy_proxy"
}

# 兜底清理：按完整启动参数精确匹配（只影响本实例，不误伤其它端口的代理）
pattern_kill() {
    pkill -9 -f "buddy_proxy --host $PROXY_HOST --port $PROXY_PORT" 2>/dev/null || true
}

usage() {
    grep '^# ' "$0" | sed 's/^# //'
    exit 1
}

parse_common() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p|--port)  PROXY_PORT="$2"; shift 2 ;;
            -H|--host)  PROXY_HOST="$2"; shift 2 ;;
            --port=*)   PROXY_PORT="${1#*=}"; shift ;;
            --host=*)   PROXY_HOST="${1#*=}"; shift ;;
            *)          EXTRA_ARGS="$EXTRA_ARGS $1"; shift ;;
        esac
    done
}

cmd_start() {
    parse_common "$@"
    # launchd 服务（com.buddy.proxy）在管本端口时禁止 nohup 野起：nohup 实例和
    # KeepAlive 会抢同一个端口，拉起即 errno 48 死循环（2026-10-06 实测 388 次
    # spawn 全灭），最后留下「探活假成功、野实例一死就全挂」的烂摊子。统一走 buddy。
    # 显式换端口（-p）不拦：8788 这类调试实例是合法用法。
    if [[ "$PROXY_PORT" == "8787" ]] \
        && [[ -f "$HOME/Library/LaunchAgents/com.buddy.proxy.plist" ]] \
        && launchctl print "gui/$UID/com.buddy.proxy" >/dev/null 2>&1; then
        log "ERROR: ${PROXY_PORT} 已由 launchd 服务 com.buddy.proxy 管理，请用 buddy start / buddy restart"
        log "（proxy.sh 野起的 nohup 实例会和 KeepAlive 抢端口，见 buddy 里 evict_port_squatters 的注释）"
        return 1
    fi
    # 以端口实况为准：真有实例在监听才算已运行
    local port_pid
    port_pid="$(find_port_pid || true)"
    if [[ -n "$port_pid" ]]; then
        if is_proxy_cmd "$port_pid"; then
            log "already running (pid=$port_pid, listening on $PROXY_HOST:$PROXY_PORT)"
            return 0
        fi
        log "ERROR: port $PROXY_PORT occupied by pid=$port_pid (not buddy-proxy):"
        ps -o command= -p "$port_pid" | head -1
        return 1
    fi

    # 端口空闲但可能有「不监听的僵尸实例」或过期 pid 文件：先清理再启动
    local old
    old="$(read_pid)"
    if [[ -n "$old" ]]; then
        if is_running "$old" && is_proxy_cmd "$old"; then
            log "cleaning hung instance pid=$old (alive but not listening)"
            kill -9 "$old" 2>/dev/null || true
            sleep 1
        elif ! is_running "$old"; then
            log "removing stale pid file (pid=$old not running)"
        fi
    fi
    pattern_kill
    rm -f "$PID_FILE"

    log "starting buddy-proxy on $PROXY_HOST:$PROXY_PORT ..."
    # nohup + & : 立刻返回，不阻塞
    # 走源文件 src/ 运行，避开本机 setuptools env 的 EEXIST 环境 bug（uv sync 构建可编辑安装时触发）
    PYTHONPATH="$SCRIPT_DIR/src" \
    nohup $PYTHON_BIN -m buddy_proxy \
        --host "$PROXY_HOST" \
        --port "$PROXY_PORT" \
        --log-file "$LOG_DIR/buddy-proxy.jsonl" \
        $EXTRA_ARGS \
        >>"$LOG_FILE" 2>&1 &
    local new_pid=$!

    echo "$new_pid" > "$PID_FILE"
    # 等最多 5s 看是否存活
    for _ in 1 2 3 4 5; do
        sleep 1
        if ! is_running "$new_pid"; then
            log "FAILED to start, see $LOG_FILE"
            rm -f "$PID_FILE"
            tail -n 20 "$LOG_FILE" || true
            return 1
        fi
    done
    log "started (pid=$new_pid), log: $LOG_FILE"
    return 0
}

cmd_stop() {
    parse_common "$@"
    local pid
    pid="$(read_pid)"
    local port_pid
    port_pid="$(find_port_pid || true)"

    if [[ -z "$pid" ]] && [[ -z "$port_pid" ]]; then
        log "not running"
        pattern_kill
        rm -f "$PID_FILE"
        return 0
    fi

    # 双保险：pid 文件指向的进程 + 真正占着端口的进程，都停
    if [[ -n "$pid" ]] && is_running "$pid"; then
        log "stopping pid=$pid ..."
        kill "$pid" 2>/dev/null || true
    fi
    if [[ -n "$port_pid" ]] && [[ "$port_pid" != "$pid" ]]; then
        log "stopping listener pid=$port_pid ..."
        kill "$port_pid" 2>/dev/null || true
    fi

    # 等端口真正释放（最多 10s）
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        port_pid="$(find_port_pid || true)"
        if [[ -z "$port_pid" ]] && ! is_running "$pid"; then
            rm -f "$PID_FILE"
            log "stopped"
            return 0
        fi
    done

    log "force killing (pid=$pid, listener=$port_pid)"
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null
    [[ -n "$port_pid" ]] && kill -9 "$port_pid" 2>/dev/null
    pattern_kill
    rm -f "$PID_FILE"
    return 0
}

cmd_restart() {
    cmd_stop || true
    sleep 1
    cmd_start "$@"
}

cmd_status() {
    local pid
    pid="$(read_pid)"
    if is_running "$pid"; then
        # 试着从 ps 里读当前端口（best-effort，失败回退到默认）
        local actual
        actual=$(ps -o command= -p "$pid" 2>/dev/null | grep -oE -- '--port[[:space:]]+[0-9]+' | awk '{print $2}' | head -1)
        local actual_host
        actual_host=$(ps -o command= -p "$pid" 2>/dev/null | grep -oE -- '--host[[:space:]]+[^[:space:]]+' | awk '{print $2}' | head -1)
        local host="${actual_host:-$PROXY_HOST}"
        local port="${actual:-$PROXY_PORT}"
        log "running (pid=$pid, $host:$port)"
        log "shell log: $LOG_FILE"
        log "jsonl log: $LOG_DIR/buddy-proxy.jsonl"
        return 0
    fi
    log "not running (pid_file=${pid:-none})"
    return 1
}

cmd_logs() {
    tail -n 100 -F "$LOG_FILE"
}

cmd_ui() {
    # 确保在跑（未跑则先启动），然后用浏览器打开管理页
    local pid
    pid="$(read_pid)"
    if ! is_running "$pid"; then
        cmd_start || return 1
    fi
    local actual
    actual=$(ps -o command= -p "$(read_pid)" 2>/dev/null | grep -oE -- '--port[[:space:]]+[0-9]+' | awk '{print $2}' | head -1)
    local url="http://127.0.0.1:${actual:-$PROXY_PORT}/ui"
    log "opening $url"
    if command -v open >/dev/null 2>&1; then
        open "$url"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url"
    else
        echo "$url"
    fi
}

cmd_login() {
    local provider="codebuddy"
    local extra=()
    # provider 白名单 = python 侧入口全集（auth/login.py 的 PROVIDER_ALIASES
    # + KNOWN_PROVIDERS）：python 认的名字这里都得放行，否则用户敲别名直接被
    # shell 拦下（codebuddyintl 实测漏过）。两边加名字要一起改，
    # tests/test_login_cli.py 的白名单同步测试会盯住。
    while [[ $# -gt 0 ]]; do
        case "$1" in
            codebuddy|codebuddyintl|cb|workbuddy|trae|traeintl|zcode|glm|doubao|dumate|mimo|qoder|qoderintl|qodercn|qoder-cn|quoder|qodor|qder|gemini|gemini-cli|antigravity|kimi|codex)
                # 别名（workbuddy/cb/quoder 系、intl 系）原样透传，python 侧归一
                provider="$1" ;;
            --no-browser)
                extra+=("$1") ;;
            # trae/qoder 选区（cn/global）：两种写法都透传给 python 侧
            --region)
                [[ $# -ge 2 ]] || { log "--region 缺少取值（cn/global）"; exit 2; }
                extra+=("$1" "$2"); shift ;;
            --region=*)
                extra+=("$1") ;;
            *)
                log "未知参数: $1"
                usage
                ;;
        esac
        shift
    done
    if [[ "$provider" == "codebuddy" ]]; then
        log "开始 codebuddy 登录（workbuddy 同义）..."
    else
        log "开始 $provider 登录...（别名由登录模块归一）"
    fi
    # 登录成功与否由 python 侧返回码决定；失败时这里原样透传非零退出码
    PYTHONPATH="$SCRIPT_DIR/src" $PYTHON_BIN -m buddy_proxy.auth.login "$provider" \
        ${extra[@]+"${extra[@]}"}
}

case "${1:-}" in
    start)   shift; cmd_start "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    restart) shift; cmd_restart "$@" ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    login)   shift; cmd_login "$@" ;;
    ui)      cmd_ui ;;
    -h|--help|help|"") usage ;;
    *)       usage ;;
esac
