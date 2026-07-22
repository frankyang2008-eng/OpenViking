#!/usr/bin/env bash
# OpenViking server + vikingbot gateway management tool.
#
# Usage:
#   ov-tools.sh start        Start the server (with --with-bot by default)
#   ov-tools.sh stop         Stop server and bot gracefully
#   ov-tools.sh restart      Stop then start
#   ov-tools.sh status       ov health
#   ov-tools.sh statusjson   ov status --verbose as JSON (machine-readable; used by netopt UI)
#   ov-tools.sh svrlogs [-f] Last 50 lines of openviking.log (-f to follow)
#   ov-tools.sh botlogs [-f] Last 50 lines of vikingbot.log (-f to follow)
#   ov-tools.sh help         Show this message
#
# All paths are env-overridable: OV_ROOT, OV_VENV, OV_CONFIG, OV_DATA_DIR,
# OV_PID_FILE, OV_LOG_DIR, OV_BOT_LOG_DIR, OV_PORT, OV_BOT_PORT, OV_WITH_BOT.
#
# This script deliberately avoids importing the Python package so it can
# stop a broken/hung server without depending on a working venv.

set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------
# Resolve symlinks so the script works when invoked via a symlink (e.g.
# ~/.openviking/script/ov-tools.sh -> repo/scripts/ov-tools.sh); otherwise
# BASH_SOURCE would point at the link and OV_ROOT would resolve wrongly.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
: "${OV_ROOT:=$(cd "$SCRIPT_DIR/.." && pwd)}"                 # scripts/ -> repo root
: "${OV_VENV:=$OV_ROOT/.venv}"
: "${OV_CONFIG:=${OPENVIKING_CONFIG_FILE:-$HOME/.openviking/ov.conf}}"
: "${OV_DATA_DIR:=$HOME/.openviking}"
: "${OV_PID_FILE:=$OV_DATA_DIR/openviking-server.pid}"
: "${OV_LOG_DIR:=$OV_DATA_DIR/data/log}"
: "${OV_BOT_LOG_DIR:=$OV_DATA_DIR/data/bot/logs}"
: "${OV_PORT:=1933}"
: "${OV_BOT_PORT:=18790}"
: "${OV_WITH_BOT:=1}"                                          # 1=always --with-bot, 0=off
export OPENVIKING_CONFIG_FILE="$OV_CONFIG"
export PATH="$OV_VENV/bin:$PATH"

OV_SERVER="$OV_VENV/bin/openviking-server"
OV_BIN="$OV_VENV/bin/ov"
SVR_LOG="$OV_LOG_DIR/openviking.log"
BOT_LOG="$OV_BOT_LOG_DIR/vikingbot.log"
STARTUP_LOG="$OV_LOG_DIR/openviking-start-$(date +%Y%m%d).log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

usage() {
    cat <<'EOF'
OpenViking server + vikingbot gateway management tool.

Usage: ov-tools.sh <command> [options]

Commands:
  start           Start the server (with --with-bot by default).
  stop            Stop the server and bot gracefully.
  restart         Stop then start.
  status          Show ov health.
  svrlogs [-f]    Last 50 lines of openviking.log (-f to follow).
  botlogs [-f]    Last 50 lines of vikingbot.log (-f to follow).
  help            Show this message.

Environment overrides:
  OV_ROOT, OV_VENV, OV_CONFIG, OV_DATA_DIR, OV_PID_FILE,
  OV_LOG_DIR, OV_BOT_LOG_DIR, OV_PORT, OV_BOT_PORT, OV_WITH_BOT
EOF
}

# Echo PID if the PID file points at a live process, else return 1.
is_running_by_pid_file() {
    if [ -f "$OV_PID_FILE" ]; then
        local pid
        pid=$(cat "$OV_PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    return 1
}

# Echo PID holding $1 (LISTEN), or empty.
pid_using_port() {
    lsof -ti:"$1" -sTCP:LISTEN 2>/dev/null
}

# Best-effort: kill an orphaned vikingbot gateway left by a force-killed
# server. The server's graceful handler already stops the bot on SIGTERM, so
# this only matters after SIGKILL. Located via the bot port to avoid
# matching unrelated processes (e.g. a `tail -f vikingbot.log`).
cleanup_orphan_bot() {
    local bot_pid
    bot_pid=$(pid_using_port "$OV_BOT_PORT")
    if [ -n "$bot_pid" ]; then
        log "Cleaning up orphaned vikingbot on port $OV_BOT_PORT (PID: $bot_pid)"
        kill "$bot_pid" 2>/dev/null || true
        sleep 1
        if [ -n "$(pid_using_port "$OV_BOT_PORT")" ]; then
            log "Force killing remaining vikingbot (PID: $bot_pid)"
            kill -9 "$bot_pid" 2>/dev/null || true
        fi
    fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
start_server() {
    local old_pid
    old_pid=$(is_running_by_pid_file)
    if [ -n "$old_pid" ]; then
        log "OpenViking server already running (PID: $old_pid)"
        return 0
    fi

    if [ -f "$OV_PID_FILE" ]; then
        log "Removing stale PID file"
        rm -f "$OV_PID_FILE"
    fi

    local existing
    existing=$(pid_using_port "$OV_PORT")
    if [ -n "$existing" ]; then
        log "ERROR: port $OV_PORT already in use by PID $existing"
        log "Run 'ov-tools.sh stop' first, or check if another service holds the port."
        return 1
    fi

    if [ ! -x "$OV_SERVER" ]; then
        log "ERROR: server binary not found or not executable: $OV_SERVER"
        log "Run 'make build' first."
        return 1
    fi

    mkdir -p "$OV_LOG_DIR" "$OV_BOT_LOG_DIR"

    log "Starting OpenViking server (config: $OV_CONFIG)..."
    # shellcheck disable=SC1091
    source "$OV_VENV/bin/activate" 2>/dev/null || true

    if [ "$OV_WITH_BOT" = "1" ]; then
        log "  --with-bot enabled (vikingbot gateway on port $OV_BOT_PORT)"
        nohup "$OV_SERVER" --with-bot >> "$STARTUP_LOG" 2>&1 &
    else
        nohup "$OV_SERVER" >> "$STARTUP_LOG" 2>&1 &
    fi
    local new_pid=$!
    echo "$new_pid" > "$OV_PID_FILE"

    log "Server started (PID: $new_pid)"
    log "Startup/stderr log: $STARTUP_LOG"
    log "Server log:         $SVR_LOG"
    log "Bot log:            $BOT_LOG"

    # Liveness check
    sleep 3
    if ! ps -p "$new_pid" > /dev/null 2>&1; then
        log "ERROR: process exited unexpectedly. Last 25 lines of startup log:"
        tail -n 25 "$STARTUP_LOG" 2>/dev/null
        rm -f "$OV_PID_FILE"
        return 1
    fi
    log "Liveness: process alive"

    # HTTP health probe (best-effort; server may still be booting)
    local code
    code=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$OV_PORT/health" 2>/dev/null || true)
    if [ "$code" = "200" ]; then
        log "HTTP health: reachable (200)"
    else
        log "HTTP health: not ready yet (http_code=${code:-none}); check 'ov-tools.sh svrlogs -f'"
    fi
}

stop_server() {
    local pid
    pid=$(is_running_by_pid_file)

    if [ -n "$pid" ]; then
        log "Stopping OpenViking server (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        local i
        for i in $(seq 1 10); do
            if ! ps -p "$pid" > /dev/null 2>&1; then
                log "Server stopped gracefully"
                break
            fi
            sleep 1
        done
        if ps -p "$pid" > /dev/null 2>&1; then
            log "Did not stop in 10s, force killing..."
            kill -9 "$pid" 2>/dev/null || true
            log "Server force stopped"
        fi
        rm -f "$OV_PID_FILE"
    elif [ -f "$OV_PID_FILE" ]; then
        log "PID file exists but process dead; cleaning up"
        rm -f "$OV_PID_FILE"
    fi

    # Best-effort orphaned bot cleanup (only meaningful after SIGKILL)
    cleanup_orphan_bot

    # Port fallback: something still holding the server port
    local port_pid
    port_pid=$(pid_using_port "$OV_PORT")
    if [ -n "$port_pid" ]; then
        local proc_name
        proc_name=$(ps -p "$port_pid" -o comm= 2>/dev/null)
        if [[ "$proc_name" == *"openviking-server"* ]] || [[ "$proc_name" == *"python"* ]]; then
            log "Stopping leftover process on port $OV_PORT (PID: $port_pid, $proc_name)..."
            kill "$port_pid" 2>/dev/null || true
            sleep 2
            if ps -p "$port_pid" > /dev/null 2>&1; then
                kill -9 "$port_pid" 2>/dev/null || true
            fi
        else
            log "Port $OV_PORT held by '$proc_name' (not openviking-server); leaving it alone"
        fi
    fi

    # Final verification
    sleep 1
    if [ -z "$(pid_using_port "$OV_PORT")" ] && [ -z "$(is_running_by_pid_file)" ]; then
        log "Stop verified: port $OV_PORT free, PID file clean"
        return 0
    fi
    log "WARNING: could not fully confirm server is stopped"
    return 1
}

restart_server() {
    stop_server || true
    start_server
}

show_status() {
    echo "== ov health =="
    if [ -x "$OV_BIN" ]; then
        "$OV_BIN" health 2>&1 || echo "(ov health failed)"
    else
        echo "(ov binary not found at $OV_BIN)"
    fi
}

# Machine-readable status for the netopt web UI. Always emits valid JSON so the
# frontend can JSON.parse it even when the ov binary is missing.
status_json() {
    if [ -x "$OV_BIN" ]; then
        "$OV_BIN" status --verbose -o json
    else
        printf '{"ok":false,"result":{"is_healthy":false,"errors":["ov binary not found at %s"],"components":{}}}\n' "$OV_BIN"
    fi
}

svrlogs() {
    if [ ! -f "$SVR_LOG" ]; then
        echo "Server log not found: $SVR_LOG" >&2
        return 1
    fi
    if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--follow" ]; then
        tail -f "$SVR_LOG"
    else
        tail -n 50 "$SVR_LOG"
    fi
}

botlogs() {
    if [ ! -f "$BOT_LOG" ]; then
        echo "Bot log not found: $BOT_LOG" >&2
        echo "(bot log is created when the server starts with --with-bot)" >&2
        return 1
    fi
    if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--follow" ]; then
        tail -f "$BOT_LOG"
    else
        tail -n 50 "$BOT_LOG"
    fi
}

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if [ $# -eq 0 ]; then
    usage
    exit 1
fi

cmd="$1"
shift || true
case "$cmd" in
    start|--start)       start_server ;;
    stop|--stop)         stop_server ;;
    restart|--restart)   restart_server ;;
    status|--status)     show_status ;;
    statusjson)          status_json ;;
    svrlogs|server-logs) svrlogs "${1:-}" ;;
    botlogs|bot-logs)    botlogs "${1:-}" ;;
    help|-h|--help)      usage ;;
    *)
        echo "Unknown command: $cmd" >&2
        usage
        exit 1
        ;;
esac
