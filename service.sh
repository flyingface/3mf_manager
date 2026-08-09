#!/usr/bin/env bash
# 3MF Manager 一键启停脚本（后台守护进程）
#
# 用法:
#   ./service.sh start     启动服务（后台）
#   ./service.sh stop      停止服务
#   ./service.sh status    查看运行状态
#   ./service.sh restart   重启服务
#   ./service.sh logs      查看日志（-f 持续跟随）
#
# 环境变量:
#   PORT    端口（默认 8000）
#   PYTHON  python 解释器（默认优先使用 .venv，其次系统 python3）

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
RUN_DIR="$DIR/run"
LOG_DIR="$DIR/logs"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$LOG_DIR/server.log"
ERR_FILE="$LOG_DIR/server.err"

# 选择 python
if [[ -x "$DIR/.venv/bin/python" ]]; then
  PYTHON="$DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "错误：未找到 python3" >&2
  exit 1
fi

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

do_start() {
  if is_running; then
    echo "服务已在运行 (PID $(cat "$PID_FILE"))"
    return 0
  fi
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  echo "启动 3MF Manager @ http://127.0.0.1:$PORT"
  # 后台运行，输出到日志
  nohup "$PYTHON" "$DIR/server.py" "$PORT" >>"$LOG_FILE" 2>>"$ERR_FILE" &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "已启动 (PID $pid)"
  else
    echo "启动失败，查看 $ERR_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

do_stop() {
  if ! is_running; then
    echo "服务未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "停止服务 (PID $pid)"
  kill "$pid" 2>/dev/null || true
  # 等待退出
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  # 强制清理
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "已停止"
}

do_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    local url="http://127.0.0.1:$PORT"
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || echo 000)"
    echo "● 运行中 (PID $pid)  HTTP $code @ $url"
  else
    echo "○ 已停止"
  fi
}

do_logs() {
  [[ -f "$LOG_FILE" ]] || { echo "日志文件不存在"; return 1; }
  if [[ "${1:-}" == "-f" ]]; then
    tail -f "$LOG_FILE"
  else
    tail -n "${2:-50}" "$LOG_FILE"
  fi
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  logs)    do_logs "${2:-}" ;;
  *)       echo "用法: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
