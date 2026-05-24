#!/usr/bin/env bash
# status.sh — V_agent 各サービスの稼働状況を確認
# 使い方: ./status.sh

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT/.pids"
LOG_DIR="$ROOT/.logs"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; B='\033[1m'; N='\033[0m'

running() { echo -e "  ${G}●${N}  $*"; }
stopped() { echo -e "  ${R}○${N}  $*"; }
warn()    { echo -e "  ${Y}△${N}  $*"; }

check_pid() {
    local name=$1 label=$2
    local f="$PID_DIR/$name.pid"
    if [ -f "$f" ]; then
        local pid; pid=$(cat "$f")
        if kill -0 "$pid" 2>/dev/null; then
            running "$label  (PID $pid)"
        else
            warn "$label  (PID $pid, 異常終了)"
        fi
    else
        stopped "$label"
    fi
}

echo ""
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo -e "${B}  V_agent サービス状態${N}"
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo ""

# VOICEVOX (native app — PID ファイルなし)
if curl -sf http://localhost:50021/version >/dev/null 2>&1; then
    v=$(curl -sf http://localhost:50021/version)
    running "VOICEVOX ${v}  (localhost:50021)"
else
    stopped "VOICEVOX  (localhost:50021)"
fi

check_pid "bridge"  "ha-character-bridge  ws://127.0.0.1:8000/ws"

# ha-bridge — HTTP チェックも追加
if [ -f "$PID_DIR/habridge.pid" ]; then
    pid=$(cat "$PID_DIR/habridge.pid")
    if kill -0 "$pid" 2>/dev/null; then
        if curl -sf http://127.0.0.1:18088/health >/dev/null 2>&1; then
            running "ha-bridge  http://127.0.0.1:18088  (PID $pid)"
        else
            warn "ha-bridge  起動中…  http://127.0.0.1:18088  (PID $pid)"
        fi
    else
        warn "ha-bridge  (PID $pid, 異常終了)"
    fi
else
    stopped "ha-bridge  http://127.0.0.1:18088"
fi

# AITuber Kit — HTTP チェックも追加
if [ -f "$PID_DIR/aituber.pid" ]; then
    pid=$(cat "$PID_DIR/aituber.pid")
    if kill -0 "$pid" 2>/dev/null; then
        if curl -sf http://localhost:3000 >/dev/null 2>&1; then
            running "AITuber Kit  http://localhost:3000  (PID $pid)"
        else
            warn "AITuber Kit  起動中…  http://localhost:3000  (PID $pid)"
        fi
    else
        warn "AITuber Kit  (PID $pid, 異常終了)"
    fi
else
    stopped "AITuber Kit  http://localhost:3000"
fi

check_pid "agent" "Pipecat Voice Agent"

echo ""
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"

# 最新ログ (agent のみ)
if [ -f "$LOG_DIR/agent.log" ]; then
    echo ""
    echo -e "${B}  Agent 最新ログ (末尾 10 行):${N}"
    echo ""
    tail -n 10 "$LOG_DIR/agent.log" | sed 's/^/    /'
fi
echo ""
