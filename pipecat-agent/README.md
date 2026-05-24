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
  → [Pipecat] Silero VAD (stop_secs=0.2s)
  → [Pipecat] WakeWordGate (Hey Kemy / openWakeWord)
  → [Pipecat] OpenAI STT gpt-4o-mini-transcribe  ← 推奨デフォルト
  → [Pipecat] Groq LLM   llama-3.3-70b-versatile ← 推奨デフォルト
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
# .env を編集して各 API キーを設定
```

## 起動

### 一括起動 (推奨)

```bash
cd /Users/shin/work/V_agent
./start.sh   # VOICEVOX は事前に起動しておく
```

### 単体起動 (デバッグ用)

```bash
cd /Users/shin/work/V_agent/pipecat-agent
.venv/bin/python agent.py
```

「ヘイ ケミー」と言ってから話しかけると応答します。

## 環境変数 (.env)

### バックエンド選択

| 変数 | 説明 | デフォルト | 選択肢 |
|---|---|---|---|
| `STT_BACKEND` | STT エンジン | `openai` | `openai` / `deepgram` |
| `LLM_BACKEND` | LLM エンジン | `groq` | `groq` / `openai` |

### API キー

| 変数 | 説明 | 必須条件 |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API キー | 常に必要 (STT) |
| `DEEPGRAM_API_KEY` | Deepgram API キー | `STT_BACKEND=deepgram` のとき |
| `GROQ_API_KEY` | Groq API キー | `LLM_BACKEND=groq` のとき |

### モデル設定

| 変数 | 説明 | デフォルト |
|---|---|---|
| `STT_MODEL` | OpenAI STT モデル | `gpt-4o-mini-transcribe` |
| `LLM_MODEL` | OpenAI LLM モデル (fallback) | `gpt-4o-mini` |
| `GROQ_MODEL` | Groq LLM モデル | `llama-3.3-70b-versatile` |
| `DEEPGRAM_MODEL` | Deepgram STT モデル | `nova-3-general` |

### その他

| 変数 | 説明 | デフォルト |
|---|---|---|
| `HA_CHAR_BRIDGE_WS_URL` | ha-character-bridge WebSocket URL | `ws://127.0.0.1:8000/ws` |
| `HA_BRIDGE_URL` | ha-bridge HTTP URL | `http://127.0.0.1:18088` |
| `WAKEWORD_MODEL_PATHS` | Wake word モデルパス (カンマ区切り) | `../voice-listener/models/Hey_Kemy.onnx` |
| `WAKE_THRESHOLD` | Wake word 検出閾値 | `0.5` |
| `WAKE_CONFIRM_CHUNKS` | 連続確認チャンク数 | `2` |
| `WAKE_ACTIVE_WINDOW` | Wake 後の STT 有効秒数 | `8.0` |
| `VAD_START_SECS` | 発話開始判定の最低継続秒数 | `0.2` |
| `VAD_STOP_SECS` | 発話終了判定の無音秒数 | `0.2` |
| `VAD_CONFIDENCE` | VAD 信頼度閾値 | `0.7` |
| `MIC_DEVICE_INDEX` | マイクデバイスインデックス (空=デフォルト) | (空) |

---

## ベンチマーク結果

### レイテンシー比較 (`bench.py`)

> 測定条件: N=3, 日本語 2 秒発話想定, 2026-05-19

| システム構成 | STT | LLM TTFT | 合計推定 |
|---|---|---|---|
| 旧システム (openclaw + voice-listener) | ~1,500ms | ~640ms | **~3,010ms** |
| Pipecat v1 (OpenAI STT + gpt-4o) | ~880ms | ~640ms | **~1,710ms** |
| Pipecat v2 (Deepgram streaming + gpt-4o) | ~250ms | ~640ms | **~1,220ms** |
| Pipecat v2 (Deepgram streaming + Groq 70b) | ~250ms | ~150ms | **~510ms** ✓ |
| **推奨構成 (OpenAI STT + Groq 70b)** | ~880ms | ~150ms | **~1,030ms** |

目標: wake word 後 → 発話開始まで **800ms 以内**。  
Deepgram STT を使えば 510ms で達成できるが、後述の日本語精度問題があるため、  
精度優先では OpenAI STT + Groq LLM (~1,030ms) が現実的な推奨構成。

主な改善要因:
- `VAD stop_secs=0.2s` (旧 0.9s から -700ms)
- `aggregation_timeout=0.3s` (pipecat デフォルト 1.0s から -700ms)
- Groq LPU による TTFT ~150ms (OpenAI gpt-4o ~640ms から -490ms)

---

### STT 精度テスト (`bench_accuracy.py`)

> 測定条件: macOS `say` (Kyoko 日本語女性 / Reed 英語男性) × 各 1 回, 10 文  
> 指標: CER (文字誤り率、低いほど良い) / 完全一致率 / HA キーワード一致率

| モデル | CER ↓ | 完全一致 ↑ | KW 一致 ↑ | 総合 |
|---|---|---|---|---|
| OpenAI gpt-4o-mini-transcribe | **1.2%** | **90%** | **90%** | ★★★★★ |
| OpenAI gpt-4o-transcribe | **1.2%** | **90%** | **90%** | ★★★★★ |
| OpenAI whisper-1 | 3.6% | 80% | 85% | ★★★★☆ |
| Deepgram nova-2-general | 57.2% | 30% | 40% | ☆☆☆☆☆ |
| Deepgram nova-3-general | 65.5% | 30% | 35% | ☆☆☆☆☆ |

#### 文ごとの詳細 (gpt-4o-transcribe vs Deepgram nova-3)

| テスト文 | カテゴリ | gpt-4o-transcribe CER | nova-3 CER |
|---|---|---|---|
| 洗面所の電気をつけてください | HA_clear | 0.0% | 50.0% |
| 洗面所の電気を消して | HA_clear | 0.0% | 100.0% |
| 電気をつけてください | HA_short | 0.0% | 50.0% |
| 電気を消してください | HA_short | 0.0% | 50.0% |
| 洗面所のライトをつけてください | HA_polite | 0.0% | 50.0% |
| こんにちは今日もよろしくお願いします | casual | 0.0% | 50.0% |
| 最近のおすすめ映画を教えてください | casual | 0.0% | 50.0% |
| 今日は少し疲れました | casual | 0.0% | 55.0% |
| ありがとうございます | short | 0.0% | 100.0% |
| はい分かりました | short | 12.5% | 100.0% |

**考察:**
- `gpt-4o-mini-transcribe` と `gpt-4o-transcribe` は同精度で、コスト的に前者が優位。
- Deepgram は日本語 CER が 57〜65% と非常に高く、短い発話・敬語での失敗が多い。
  速度 (~250ms streaming) は魅力的だが、**日本語音声認識には現時点で非推奨**。
- OpenAI の失敗例は「はい分かりました」のみ (CER=12.5%、Reed 英語音声起因の可能性あり)。

---

### LLM 応答精度テスト (`bench_accuracy.py`)

> 測定条件: 10 プロンプト (HA 操作 6 件 + casual 4 件) × 各 4 モデル, 2026-05-19  
> 指標: HA ツールコール正解率 / 正しいアクション名 / 誤呼出率 (casual で誤って HA 呼ぶ) / 日本語率

| モデル | HA 呼出正解 ↑ | 正 Action ↑ | 誤呼出 ↓ | 日本語率 ↑ | 総合 |
|---|---|---|---|---|---|
| OpenAI gpt-4o-mini | **100%** | **100%** | **0%** | 100% | ★★★★★ |
| Groq llama-3.1-8b-instant | **100%** | **100%** | **0%** | 100% | ★★★★★ |
| Groq llama-3.3-70b-versatile | **100%** | **100%** | **0%** | 100% | ★★★★★ |
| OpenAI gpt-4o | 66.7% | 66.7% | 0% | 100% | ★★★★☆ |

#### プロンプトごとの詳細 (gpt-4o vs Groq llama-3.3-70b)

| プロンプト | 期待 | gpt-4o | llama-3.3-70b |
|---|---|---|---|
| 洗面所の電気をつけて | `bathroom_light_on` | ✓ | ✓ |
| 洗面所の電気を消して | `bathroom_light_off` | ✓ | ✓ |
| 電気つけて | `bathroom_light_on` | **✗ no_call** | ✓ |
| 電気消して | `bathroom_light_off` | **✗ no_call** | ✓ |
| 洗面所のライトをつけていただけますか | `bathroom_light_on` | ✓ | ✓ |
| 洗面所の照明を消していただけますか | `bathroom_light_off` | ✓ | ✓ |
| こんにちは！ | (casual) | 日本語 ✓ | 日本語 ✓ |
| 今日の天気はどうですか | (casual) | 日本語 ✓ | 日本語 ✓ |
| 最近疲れています | (casual) | 日本語 ✓ | 日本語 ✓ |
| おすすめの映画を教えて | (casual) | 日本語 ✓ | 日本語 ✓ |

**考察:**
- `gpt-4o` は場所を省略した短い命令「電気つけて/消して」でツールコールせず失敗。
  `gpt-4o-mini` や Groq 全モデルはすべて正解。
- `llama-3.1-8b-instant` (最速) と `llama-3.3-70b-versatile` (高品質) は同スコア。
  速度優先なら 8b、品質安定性を重視するなら 70b を選択。
- 誤呼出率はすべて 0% — casual な会話で誤って家電を操作するリスクはなし。

---

### 推奨構成まとめ

| 優先 | STT | LLM | 推定レイテンシー | 日本語精度 |
|---|---|---|---|---|
| **精度 + バランス (デフォルト)** | OpenAI gpt-4o-mini-transcribe | Groq llama-3.3-70b | ~1,030ms | CER 1.2% / HA 100% |
| 最速 (精度妥協) | Deepgram nova-3 | Groq llama-3.1-8b | ~400ms | CER 65% / HA 100% |
| 高品質 (コスト高) | OpenAI gpt-4o-transcribe | OpenAI gpt-4o-mini | ~1,080ms | CER 1.2% / HA 100% |
| ※ 非推奨 | — | OpenAI gpt-4o | — | HA 67% (短命令で失敗) |

---

## ベンチマーク実行方法

```bash
# レイテンシーベンチマーク
.venv/bin/python bench.py

# STT 精度 + LLM 精度ベンチマーク (Kyoko voice のみ、1 回)
.venv/bin/python bench_accuracy.py --voices Kyoko --reps 1

# フルテスト (Kyoko + Reed, 2 回)
.venv/bin/python bench_accuracy.py --voices Kyoko Reed --reps 2
```

---

## openclaw との共存

- `openclaw/` ディレクトリはそのまま残ります
- `ha-bridge` コンテナは pipecat-agent からも直接呼び出せます
- pipecat-agent を起動した場合、voice-listener と interaction-bridge は不要です

## HA アクション追加

`ha_tools.py` の `HA_TOOL_DEFINITIONS` の `enum` と、
`openclaw/ha-bridge/devices.yaml` の両方に追記してください。

---

## v3 改善履歴 (2026-05)

### WakeWordGate の改善

| 問題 | 原因 | 修正 |
|---|---|---|
| ウェイクワード音声が STT に渡っていた | `_pending_started_frame` のリプレイ機構がウェイクワード中の音声を STT に流していた | ゲート開放時にペンディングフレームを破棄するよう変更 |
| ゲートが開いても無反応でユーザーが繰り返す | 開放フィードバックなし | ゲート開放時に「はい？」を AITuber Kit に即時送信 |
| AIのスピーカー音をマイクが拾ってエコーループ | VOICEVOX発話中もマイクが有効 | AI応答送信後に `suppress_mic(N秒)` でマイク抑制 |
| 2問目以降にウェイクワードが毎回必要 | `active_window` (8秒) が発話+応答で使い切れる | AI応答後に `extend_gate()` でウィンドウをリセット |
| 短い言葉が誤認識される (元気→緊急 等) | Whisper に文脈がない | `prompt=` に日常語彙ヒントを渡すよう修正 |

### AITuberSink の改善

**文単位ストリーミング** — LLM応答の全文が揃うまで待たずに、`。！？` が来た時点で即座に送信。

```
変更前: LLM生成完了 (0.5〜1s) → まとめて送信 → VOICEVOX開始
変更後: 第1文の句点が来た瞬間に送信 → VOICEVOX開始 (0.5〜1s 短縮)
```

複数文は内部 asyncio.Queue で順番通りに送信されるため、再生順は保証されます。

**マイク抑制タイミングの精度改善** — 第1文を送信した時刻を記録し、LLM応答完了時点での経過時間を引いた残り再生時間だけ抑制。

```
変更前: 全文字数 / 6 + 2s (再生済み分も二重カウント)
変更後: max(2s, 全再生時間 - 第1文送信からの経過時間)
```

### ha-bridge のローカル起動対応

`openclaw/ha-bridge/main.py` の `/app/devices.yaml` (Docker専用パス) を `Path(__file__).parent / "devices.yaml"` に変更。
`start.sh` が初回実行時に自動で venv を作成し、`openclaw/ha-bridge/.env` の認証情報を読み込んでポート 18088 で起動します。

### 一括起動スクリプト

| スクリプト | 役割 |
|---|---|
| `start.sh` | VOICEVOX確認 → bridge → ha-bridge → AITuber Kit → Agent を順番に起動 |
| `stop.sh` | 全サービスを PID ファイルで管理して停止 |
| `status.sh` | 各サービスの稼働状況を表示 |

### システムプロンプトの改善

LLMが不要な家電操作ツールを呼び出す問題に対して:

- 雑談・感情の話では絶対にツールを呼び出さないよう明示
- ツールがエラーになった場合は1回だけ報告して再試行しないよう明示
- 返答は1〜2文で簡潔にするよう指示
