#!/usr/bin/env bash
# start.sh — V_agent 全サービスを一括起動
# 使い方: ./start.sh
# 終了:   Ctrl+C (全サービスを自動停止)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/.logs"
PID_DIR="$ROOT/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

B='\033[1m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; N='\033[0m'
log()  { echo -e "${C}▶${N}  $*"; }
ok()   { echo -e "${G}✓${N}  $*"; }
warn() { echo -e "${Y}⚠${N}  $*"; }
fail() { echo -e "${R}✗${N}  $*"; exit 1; }

# --- 既に起動中のチェック ---
if [ -f "$PID_DIR/agent.pid" ] && kill -0 "$(cat "$PID_DIR/agent.pid")" 2>/dev/null; then
    warn "既に起動中です。先に ./stop.sh を実行してください。"
    exit 1
fi

echo ""
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo -e "${B}  V_agent 起動スクリプト${N}"
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo ""

# --- VOICEVOX チェック ---
log "VOICEVOX を確認中 (localhost:50021)..."
if curl -sf http://localhost:50021/version >/dev/null 2>&1; then
    version=$(curl -sf http://localhost:50021/version)
    ok "VOICEVOX ${version} 起動済み"
else
    warn "VOICEVOX が起動していません"
    echo "  → VOICEVOX.app を起動してから Enter を押してください"
    echo "  → スキップする場合も Enter (音声合成なしで動作)"
    read -r
    if curl -sf http://localhost:50021/version >/dev/null 2>&1; then
        ok "VOICEVOX 確認完了"
    else
        warn "VOICEVOX なしで続行します"
    fi
fi
echo ""

TAIL_PID=""

stop_all() {
    echo ""
    log "全サービスを停止中..."
    [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
    for name in agent aituber bridge habridge; do
        f="$PID_DIR/$name.pid"
        [ -f "$f" ] || continue
        pid=$(cat "$f")
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            sleep 0.3
            kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
            ok "$name 停止 (PID $pid)"
        fi
        rm -f "$f"
    done
    pkill -f "next-server" 2>/dev/null || true
    pkill -f "next dev"    2>/dev/null || true
    echo ""
    ok "全サービス停止完了"
}
trap stop_all EXIT INT TERM

# =========================================================
# 1. ha-character-bridge
# =========================================================
log "ha-character-bridge 起動中..."
python3 "$ROOT/ha-character-bridge/bridge_server.py" \
    > "$LOG_DIR/bridge.log" 2>&1 &
BRIDGE_PID=$!
echo "$BRIDGE_PID" > "$PID_DIR/bridge.pid"
sleep 0.5
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    fail "bridge 起動失敗 → $LOG_DIR/bridge.log を確認してください"
fi
ok "ha-character-bridge  PID=$BRIDGE_PID  ws://127.0.0.1:8000/ws"

# =========================================================
# 2. ha-bridge (Home Assistant REST → HA API)
# =========================================================
log "ha-bridge 起動中 (port 18088)..."
HA_BRIDGE_DIR="$ROOT/openclaw/ha-bridge"

# venv の作成 (初回のみ)
if [ ! -f "$HA_BRIDGE_DIR/.venv/bin/uvicorn" ]; then
    log "ha-bridge 用 venv を作成中 (初回のみ)..."
    python3 -m venv "$HA_BRIDGE_DIR/.venv"
    "$HA_BRIDGE_DIR/.venv/bin/pip" install -q -r "$HA_BRIDGE_DIR/requirements.txt"
    ok "ha-bridge venv 作成完了"
fi

# HA_URL / HA_TOKEN を ha-bridge/.env から読み込む
_HA_URL=$(grep "^HA_URL=" "$HA_BRIDGE_DIR/.env" 2>/dev/null | cut -d= -f2- || true)
_HA_TOKEN=$(grep "^HA_TOKEN=" "$HA_BRIDGE_DIR/.env" 2>/dev/null | cut -d= -f2- || true)

if [ -z "$_HA_URL" ] || [ -z "$_HA_TOKEN" ]; then
    warn "ha-bridge .env に HA_URL または HA_TOKEN がありません → HA 操作は無効"
else
    env HA_URL="$_HA_URL" HA_TOKEN="$_HA_TOKEN" \
        bash -c "cd '$HA_BRIDGE_DIR' && exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 18088" \
        > "$LOG_DIR/habridge.log" 2>&1 &
    HA_BRIDGE_PID=$!
    echo "$HA_BRIDGE_PID" > "$PID_DIR/habridge.pid"
    sleep 1
    if ! kill -0 "$HA_BRIDGE_PID" 2>/dev/null; then
        warn "ha-bridge 起動失敗 → $LOG_DIR/habridge.log を確認してください"
    elif curl -sf http://127.0.0.1:18088/health >/dev/null 2>&1; then
        ok "ha-bridge  PID=$HA_BRIDGE_PID  http://127.0.0.1:18088"
    else
        warn "ha-bridge 起動中... (health check 未応答)"
    fi
fi

# =========================================================
# 3. AITuber Kit
# =========================================================
log "AITuber Kit 起動中 (npm run dev)..."
cd "$ROOT/aituber-kit"
npm run dev > "$LOG_DIR/aituber.log" 2>&1 &
AITUBER_PID=$!
echo "$AITUBER_PID" > "$PID_DIR/aituber.pid"
cd "$ROOT"
ok "AITuber Kit  PID=$AITUBER_PID  → http://localhost:3000"

log "AITuber Kit の起動を待機中 (最大 90 秒)..."
WAITED=0
until curl -sf http://localhost:3000 >/dev/null 2>&1; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ "$WAITED" -ge 90 ]; then
        warn "タイムアウト。AITuber Kit ログ: $LOG_DIR/aituber.log"
        break
    fi
done
[ "$WAITED" -lt 90 ] && ok "AITuber Kit 起動完了 (${WAITED}秒)"

# =========================================================
# 4. Pipecat Voice Agent
# =========================================================
log "Pipecat Voice Agent 起動中..."
AGENT_PYTHON="$ROOT/pipecat-agent/.venv/bin/python"
[ -f "$AGENT_PYTHON" ] || fail "仮想環境が見つかりません: $AGENT_PYTHON"

"$AGENT_PYTHON" "$ROOT/pipecat-agent/agent.py" \
    > "$LOG_DIR/agent.log" 2>&1 &
AGENT_PID=$!
echo "$AGENT_PID" > "$PID_DIR/agent.pid"
sleep 1
if ! kill -0 "$AGENT_PID" 2>/dev/null; then
    fail "agent 起動失敗 → $LOG_DIR/agent.log を確認してください"
fi
ok "Pipecat Agent  PID=$AGENT_PID"

# =========================================================
# 起動完了
# =========================================================
echo ""
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo -e "${G}${B}  全サービス起動完了${N}"
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo ""
echo "  AITuber Kit  →  http://localhost:3000"
echo "  Bridge       →  ws://127.0.0.1:8000/ws"
echo "  HA Bridge    →  http://127.0.0.1:18088"
echo "  ログ         →  .logs/ ディレクトリ"
echo ""
echo "  Ctrl+C で全サービスを停止"
echo ""
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo -e "  ログ出力 (bridge / habridge / aituber / agent):"
echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}"
echo ""

# habridge.log は起動しなかった場合存在しないので個別にチェック
TAIL_FILES=("$LOG_DIR/bridge.log" "$LOG_DIR/aituber.log" "$LOG_DIR/agent.log")
[ -f "$LOG_DIR/habridge.log" ] && TAIL_FILES=("$LOG_DIR/habridge.log" "${TAIL_FILES[@]}")

tail -n 0 -f "${TAIL_FILES[@]}" &
TAIL_PID=$!

# プロセスが落ちていないか監視
while true; do
    sleep 5
    if ! kill -0 "$AGENT_PID" 2>/dev/null; then
        warn "Pipecat Agent が予期せず停止しました → $LOG_DIR/agent.log"
        break
    fi
done
