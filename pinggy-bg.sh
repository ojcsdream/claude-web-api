#!/usr/bin/env bash

PORT=8000
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/pinggy_${PORT}.pid"
LOGFILE="$DIR/pinggy_${PORT}.log"
URLFILE="$DIR/pinggy_${PORT}.url"

SSH_BASE=(
  ssh
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -p 443
  -R0:localhost:${PORT}
  a.pinggy.io
)

extract_url() {
    grep -Eo 'https://[^[:space:]]+' "$LOGFILE" 2>/dev/null | grep -E 'pinggy|a\.pinggy' | tail -n 1
}

runner() {
    trap 'exit 0' TERM INT

    if [ "$(id -u)" -eq 0 ]; then
        NICE_LEVEL="-5"
    else
        NICE_LEVEL="0"
    fi

    renice -n "$NICE_LEVEL" -p $$ >/dev/null 2>&1 || true

    while true; do
        echo "=============================="
        echo "启动时间：$(date)"
        echo "正在连接 Pinggy..."
        echo "本机地址：http://127.0.0.1:${PORT}"

        "${SSH_BASE[@]}" &
        SSH_PID=$!

        renice -n "$NICE_LEVEL" -p "$SSH_PID" >/dev/null 2>&1 || true

        wait "$SSH_PID"

        echo "连接断开，3 秒后自动重连：$(date)"
        sleep 3
    done
}

start() {
    if [ -f "$PIDFILE" ]; then
        OLD_PID="$(cat "$PIDFILE")"
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Pinggy 已经在后台运行。PID: $OLD_PID"

            URL="$(cat "$URLFILE" 2>/dev/null)"
            if [ -n "$URL" ]; then
                echo
                echo "公网链接："
                echo "$URL"
            else
                URL="$(extract_url)"
                if [ -n "$URL" ]; then
                    echo "$URL" > "$URLFILE"
                    echo
                    echo "公网链接："
                    echo "$URL"
                else
                    echo
                    echo "暂未读取到链接，查看日志："
                    echo "./pinggy-bg.sh log"
                fi
            fi
            exit 0
        fi
    fi

    : > "$LOGFILE"
    rm -f "$URLFILE"

    echo "正在后台启动 Pinggy..."
    echo "本机地址：http://127.0.0.1:${PORT}"

    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "$0" __runner >> "$LOGFILE" 2>&1 &
    else
        nohup "$0" __runner >> "$LOGFILE" 2>&1 &
    fi

    PID=$!
    echo "$PID" > "$PIDFILE"

    if [ "$(id -u)" -eq 0 ]; then
        renice -n -5 -p "$PID" >/dev/null 2>&1 || true
    fi

    echo "后台 PID：$PID"
    echo
    echo "正在等待 Pinggy 生成公网链接..."

    for i in $(seq 1 30); do
        URL="$(extract_url)"
        if [ -n "$URL" ]; then
            echo "$URL" > "$URLFILE"
            echo
            echo "启动成功！公网链接："
            echo "$URL"
            echo
            echo "把这个链接发给别人即可访问你的本机页面。"
            echo "本机对应地址：http://127.0.0.1:${PORT}"
            exit 0
        fi
        sleep 1
    done

    echo
    echo "已后台启动，但 30 秒内没有读取到链接。"
    echo "你可以查看日志："
    echo
    echo "./pinggy-bg.sh log"
    echo
    echo "最近日志："
    tail -n 40 "$LOGFILE"
}

stop() {
    if [ ! -f "$PIDFILE" ]; then
        echo "未运行。"
        exit 0
    fi

    PID="$(cat "$PIDFILE")"

    if kill -0 "$PID" 2>/dev/null; then
        echo "正在停止 Pinggy，PID: $PID"

        kill -TERM "-$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null
        sleep 1
        kill -KILL "-$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null

        rm -f "$PIDFILE" "$URLFILE"
        echo "已停止。"
    else
        rm -f "$PIDFILE" "$URLFILE"
        echo "进程不存在，已清理。"
    fi
}

status() {
    if [ ! -f "$PIDFILE" ]; then
        echo "未运行。"
        exit 0
    fi

    PID="$(cat "$PIDFILE")"

    if kill -0 "$PID" 2>/dev/null; then
        echo "正在运行。PID: $PID"
        echo

        URL="$(cat "$URLFILE" 2>/dev/null)"
        if [ -z "$URL" ]; then
            URL="$(extract_url)"
        fi

        if [ -n "$URL" ]; then
            echo "公网链接："
            echo "$URL"
            echo
        fi

        ps -o pid,ppid,ni,stat,command -p "$PID" 2>/dev/null || ps -p "$PID"
    else
        echo "PID 文件存在，但进程不存在。"
    fi
}

log() {
    if [ ! -f "$LOGFILE" ]; then
        echo "暂无日志。"
        exit 0
    fi

    URL="$(cat "$URLFILE" 2>/dev/null)"
    if [ -z "$URL" ]; then
        URL="$(extract_url)"
    fi

    if [ -n "$URL" ]; then
        echo "公网链接："
        echo "$URL"
        echo
        echo "最近日志："
    fi

    tail -n 80 "$LOGFILE"
}

case "$1" in
    start|"")
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 1
        start
        ;;
    status)
        status
        ;;
    log)
        log
        ;;
    __runner)
        runner
        ;;
    *)
        echo "用法："
        echo "  ./pinggy-bg.sh start    启动并直接输出链接"
        echo "  ./pinggy-bg.sh stop     停止"
        echo "  ./pinggy-bg.sh restart  重启"
        echo "  ./pinggy-bg.sh status   查看状态和链接"
        echo "  ./pinggy-bg.sh log      查看日志和链接"
        ;;
esac
