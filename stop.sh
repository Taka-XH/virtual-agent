#!/usr/bin/env bash
# stop.sh — V_agent 全サービスを停止
# 使い方: ./stop.sh

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT/.pids"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; N='\033[0m'
log()  { echo -e "${C}▶${N}  $*"; }
ok()   { echo -e "${G}✓${N}  $*"; }
warn() { echo -e "${Y}⚠${N}  $*"; }

echo ""
log "V_agent サービスを停止中..."
echo ""

stopped=0
for name in agent aituber bridge habridge; do
    f="$PID_DIR/$name.pid"
    if [ ! -f "$f" ]; then
        echo -e "  ${Y}—${N}  $name (PID ファイルなし)"
        continue
    fi
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        sleep 0.5
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
        ok "$name 停止  (PID $pid)"
        stopped=$((stopped + 1))
    else
        warn "$name は既に停止済み  (PID $pid)"
    fi
    rm -f "$f"
done

# Next.js ゾンビプロセス対策
pkill -f "next-server" 2>/dev/null && ok "next-server 停止" || true
pkill -f "next dev"    2>/dev/null || true

echo ""
if [ "$stopped" -eq 0 ]; then
    warn "停止するサービスが見つかりませんでした"
    echo "  (./start.sh で起動してください)"
else
    ok "完了 (${stopped} 件停止)"
fi
echo ""
