# pipecat-agent

Pipecat を使った Voice AI エージェント。openclaw + voice-listener を置き換えます。

## なぜ Pipecat か

| 旧システム (openclaw + voice-listener) | 新システム (pipecat-agent) |
|---|---|
| 録音 → STT → LLM → 発話 の逐次処理 | VAD・STT・LLM がパイプライン化されリアルタイム処理 |
| END_SILENCE_SECONDS (0.9s) 待ってから STT | VAD が発話終了を検出した瞬間に STT 送信 |
| interaction-bridge → openclaw gateway 経由 | 直接 ha-character-bridge に WebSocket 送信 |
| Docker サービスが複数必要 | ローカル Python 1 プロセスで動作 |

## 全体構成

```text
マイク
  → [Pipecat] Silero VAD
  → [Pipecat] WakeWordGate (Hey Kemy)
  → [Pipecat] OpenAI STT (gpt-4o-transcribe)
  → [Pipecat] OpenAI LLM (gpt-4o, HA tool calling)
  → [Pipecat] AITuberSink
  → ha-character-bridge WebSocket (ws://127.0.0.1:8000/ws)
  → AITuber Kit → VOICEVOX
```

Home Assistant 操作は LLM の function calling で直接 ha-bridge HTTP API を叩きます。

## セットアップ

```bash
cd /Users/shin/work/V_agent/pipecat-agent
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# .env を編集して OPENAI_API_KEY を設定
```

## 起動

以下を先に起動しておく:

1. AITuber Kit: `cd aituber-kit && npm run dev`
2. ha-character-bridge: `cd ha-character-bridge && .venv/bin/python bridge_server.py`
3. VOICEVOX
4. ha-bridge (HA 操作が必要な場合): `cd openclaw && docker compose up -d ha-bridge`

pipecat-agent を起動:

```bash
cd /Users/shin/work/V_agent/pipecat-agent
.venv/bin/python agent.py
```

「ヘイ ケミー」と言ってから話しかけると応答します。

## 環境変数 (.env)

| 変数 | 説明 | デフォルト |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API キー | (必須) |
| `LLM_MODEL` | 使用する LLM モデル | `gpt-4o` |
| `STT_MODEL` | 使用する STT モデル | `gpt-4o-transcribe` |
| `HA_CHAR_BRIDGE_WS_URL` | ha-character-bridge WebSocket URL | `ws://127.0.0.1:8000/ws` |
| `HA_BRIDGE_URL` | ha-bridge HTTP URL | `http://127.0.0.1:18088` |
| `WAKEWORD_MODEL_PATHS` | Wake word モデルパス (カンマ区切り) | `../voice-listener/models/Hey_Kemy.onnx` |
| `WAKE_THRESHOLD` | Wake word 検出閾値 | `0.5` |
| `WAKE_CONFIRM_CHUNKS` | 連続確認チャンク数 | `2` |
| `WAKE_ACTIVE_WINDOW` | Wake 後の STT 有効秒数 | `8.0` |
| `MIC_DEVICE_INDEX` | マイクデバイスインデックス (空=デフォルト) | (空) |

## openclaw との共存

- `openclaw/` ディレクトリはそのまま残ります
- `ha-bridge` コンテナは pipecat-agent からも直接呼び出せます
- pipecat-agent を起動した場合、voice-listener と interaction-bridge は不要です

## HA アクション追加

`ha_tools.py` の `HA_TOOL_DEFINITIONS` の `enum` と、
`openclaw/ha-bridge/devices.yaml` の両方に追記してください。
