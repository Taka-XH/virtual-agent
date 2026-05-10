# V_agent

自宅AIキャラクター環境をまとめて管理するための monorepo です。

このリポジトリには、OpenClaw、AITuber Kit、Home Assistant 連携ブリッジ、音声入力/発話用ブリッジ、AITuber Kit へメッセージを送る WebSocket ブリッジが入っています。

目的は、音声やテキストを OpenClaw に渡し、OpenClaw が内容を判断して、カジュアルトークなら自然に応答し、家電操作なら Home Assistant を操作して、結果を AITuber Kit のキャラクターに発話させることです。

## 全体構成

```text
ブラウザ録音 / voice-listener / テキスト入力
  -> openclaw/interaction-bridge
  -> OpenClaw gateway
  -> カジュアルトークなら OpenClaw が応答
  -> 家電操作なら openclaw/ha-bridge -> Home Assistant
  -> openclaw/interaction-bridge /speak または assistant_text
  -> ha-character-bridge WebSocket -> AITuber Kit -> VOICEVOX
```

## ディレクトリ構成

```text
.
├── openclaw/
│   ├── docker-compose.yml
│   ├── docker-compose.override.yml
│   ├── ha-bridge/
│   └── interaction-bridge/
├── aituber-kit/
├── ha-character-bridge/
├── voice-listener/
├── README.md
└── AGENTS.md
```

### openclaw

OpenClaw gateway と、ローカル連携用の Docker サービスを動かします。

主なローカル追加要素:

- `openclaw/ha-bridge`: 許可済みの Home Assistant 操作だけを公開するブリッジ
- `openclaw/interaction-bridge`: ブラウザ音声/テキストを受け取り OpenClaw へ渡し、AITuber Kit へ発話イベントを送るブリッジ
- `openclaw/docker-compose.override.yml`: ローカル用 Docker サービスと実行時設定

### aituber-kit

AIキャラクターの画面です。外部連携イベントを受け取り、設定された音声エンジンで発話します。

通常のURL:

```text
http://localhost:3000
```

### ha-character-bridge

`interaction-bridge` から AITuber Kit へメッセージを送るための小さな WebSocket ブリッジです。

通常のURL:

```text
ws://127.0.0.1:8000/ws
```

### voice-listener

ローカルマイクで wake word を待ち受け、発話を STT して `interaction-bridge /send-text` へ送る Python クライアントです。

現在の主な設定:

- wake word: `Hey_Kemy` (`voice-listener/models/Hey_Kemy.onnx`)
- STT: `gpt-4o-transcribe`
- 最後の録音デバッグ: `/tmp/voice-listener-last.wav`
- マイクゲイン: `MIC_GAIN`

## サービスとURL

| サービス | ホスト側URL | Docker内部URL |
| --- | --- | --- |
| AITuber Kit | `http://localhost:3000` | なし |
| OpenClaw gateway | `http://127.0.0.1:18789` | `http://openclaw-gateway:18789` |
| ha-bridge | `http://127.0.0.1:18088` | `http://ha-bridge:8088` |
| interaction-bridge | `http://127.0.0.1:18089` | `http://interaction-bridge:8090` |
| ha-character-bridge | `ws://127.0.0.1:8000/ws` | `ws://host.docker.internal:8000/ws` |
| VOICEVOX | `http://127.0.0.1:50021` | `http://host.docker.internal:50021` |

## 起動方法

### 1. AITuber Kit を起動

```bash
cd /Users/shin/work/V_agent/aituber-kit
npm run dev
```

ブラウザで開きます。

```text
http://localhost:3000
```

### 2. キャラクター用 WebSocket ブリッジを起動

```bash
cd /Users/shin/work/V_agent/ha-character-bridge
.venv/bin/python bridge_server.py
```

### 3. VOICEVOX を起動

VOICEVOX アプリなどを起動し、以下でアクセスできる状態にします。

```text
http://127.0.0.1:50021
```

Docker コンテナ内からは以下のURLでアクセスします。

```text
http://host.docker.internal:50021
```

### 4. OpenClaw 関連サービスを起動

```bash
cd /Users/shin/work/V_agent/openclaw
docker compose up -d openclaw-gateway ha-bridge interaction-bridge
```

状態確認:

```bash
docker compose ps
```

## ブラウザ音声入力

interaction bridge のページを開きます。

```text
http://127.0.0.1:18089
```

録音ボタンやテキスト入力を使うと、以下の流れで処理されます。

1. ブラウザで音声を録音
2. `interaction-bridge` が音声を文字起こし
3. `ORCHESTRATION_ENABLED=true` なら、router が実行経路を判断
4. 明確な家電操作は `ha-bridge` へ fast path で送信
5. 軽い雑談は `interaction-bridge` が direct LLM で短く返答し、AITuber Kit へ発話
6. OpenClaw の memory / skill / persona / 深い推論が必要な入力は OpenClaw に送信
7. `/speak` が呼ばれた場合は、その発話を優先して二重発話を避ける

## オーケストレーション実験

`experiment/voice-listener-metrics` ブランチでは、`interaction-bridge` に軽量オーケストレーションを追加しています。

目的:

```text
明確な家電操作 -> 低遅延で ha-bridge
軽い雑談 -> local quick reply / direct casual chat
記憶・skill・深い推論が必要な依頼 -> OpenClaw
```

設定:

```text
ORCHESTRATION_ENABLED=false
ORCHESTRATOR_LLM_ENABLED=true
ORCHESTRATOR_MODEL=gpt-4o-mini
ORCHESTRATOR_TIMEOUT_SECONDS=2.5
HA_BRIDGE_URL=http://ha-bridge:8088
CASUAL_CHAT_ENABLED=true
CASUAL_CHAT_MODEL=gpt-4o-mini
CASUAL_CHAT_TIMEOUT_SECONDS=8
CASUAL_CHAT_MAX_HISTORY_MESSAGES=8
MEMORY_DB_PATH=/home/node/.openclaw/interaction-bridge-memory.sqlite3
OPENCLAW_SESSION_TUNING_ENABLED=false
```

`ORCHESTRATION_ENABLED=true` にすると、まずローカルルールで明らかな操作を判定します。判定できない場合は「ありがとう」「ただいま」「疲れた」などの小さな quick reply を即返答します。「前に話した」「覚えておいて」「調べて」のように OpenClaw が必要な語句は OpenClaw へ直行します。「さっき」「今の流れ」「最近の会話」のような直近履歴参照は LLM router を待たず `memory_chat` に入ります。それ以外だけ軽量LLM router に聞き、許可済み action に高信頼で一致した場合だけ `ha-bridge` を直接呼びます。router がタイムアウトしても OpenClaw 必須語句でなければ direct casual chat にフォールバックします。軽い雑談は `quick_reply` または `memory_chat` として direct LLM が返答し、OpenClaw の memory / skill / persona / 深い推論が必要なものは OpenClaw へ渡します。

安全上、router が自由な Home Assistant service を呼ぶことはありません。実行できるのは `ha-bridge /actions` に出てくる許可済み action だけです。

実験ブランチで有効化して起動する例:

```bash
cd /Users/shin/work/V_agent_voice-listener-metrics/openclaw
ORCHESTRATION_ENABLED=true docker compose up -d --no-deps --build interaction-bridge
```

動作確認:

```bash
curl -X POST http://127.0.0.1:18089/send-text \
  -H "Content-Type: application/json" \
  -d '{"text":"洗面所の電気をつけて"}'
```

期待される応答は `route: "home_action"` です。軽い雑談の場合は `route: "quick_reply"` または `route: "memory_chat"`、OpenClaw が必要な入力は `route: "openclaw"` になります。

### 独自オーケストレーションの方針

このブランチでは OpenClaw へ寄せる実験とは分けて、`interaction-bridge` を低遅延 runtime として育てます。ただし OpenClaw の memory、persona、skill、モデル設定を捨てるのではなく、入力ごとに使い分けます。

```text
明確な家電操作
  -> interaction-bridge -> ha-bridge

軽い相槌・記憶不要の短い雑談
  -> interaction-bridge -> direct LLM -> AITuber Kit

過去履歴・好み・persona・tool/skill が必要な会話
  -> OpenClaw
```

router は `home_action` / `quick_reply` / `memory_chat` / `openclaw` のような経路を返す想定です。`needs_memory`、`needs_tools`、`should_remember`、`openclaw_profile` も返し、OpenClaw に送る場合は必要に応じて `sessions.patch` でセッションの `model`、`thinkingLevel`、`fastMode`、`reasoningLevel` を調整します。

判断の目安:

- `quick_reply`: 「ありがとう」「ただいま」「一言で励まして」など、履歴がなくても自然に返せるもの
- `memory_chat`: 「さっき」「今の流れ」「最近の会話」など、直近の会話履歴だけで十分な雑談
- `openclaw`: 「前に話した件」「いつもの設定」「覚えておいて」「調べて」「複数 tool が必要」など、OpenClaw の memory / skill / persona を使うべきもの

長期記憶の要約更新は `should_remember=true` の時だけ行います。単なる `memory_chat` は短期履歴を参照するだけで、安定した好みとしては保存しません。

会話継続:

`interaction-bridge` は返答が質問や提案で終わる場合、レスポンスに `conversation.continue_listening=true` を含めます。`voice-listener` はこのフラグを見ると wake word に戻らず、短時間だけ follow-up 発話を待ちます。

```text
FOLLOW_UP_ENABLED=true
FOLLOW_UP_MAX_TURNS=2
FOLLOW_UP_RECORD_SECONDS=8
FOLLOW_UP_COOLDOWN_SECONDS=0.4
```

これにより、毎回 `Hey_Kemy` を言い直さずに短い会話ループを続けられます。

OpenClaw 自体を速くする案:

- router で簡単な依頼は `fastMode=true`、`thinkingLevel=off`、`reasoningLevel=off` に寄せる
- 深い推論が必要な依頼だけ強いモデルや thinking を使う
- 履歴参照が不要な依頼は別セッションや短い履歴のセッションへ逃がす
- OpenClaw の `chat.send` は非同期 ack なので、必要なら「先に短い相槌を発話し、OpenClaw の本回答を待つ」体感速度改善も検討する

現在の実験では、OpenClaw メインセッションを `gpt-5.5` から `gpt-4.1-mini` に切り替え、`thinkingLevel=off`、`fastMode=true`、`reasoningLevel=off` で低遅延側に寄せています。`gpt-4.1-nano` も試しましたが、現在の OpenClaw tool payload では `web_search_preview` 非対応のため通常 turn が失敗しました。

## 常時待受音声入力

`voice-listener` を使うと、ブラウザ録音ボタンを押さずにローカルマイクで wake word 待受できます。

初回セットアップ:

```bash
cd /Users/shin/work/V_agent/voice-listener
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

`.env` に `OPENAI_API_KEY` を設定します。必要に応じて `MIC_DEVICE_INDEX` と `MIC_GAIN` を調整してください。
雑音を音声として拾いやすい場合は `VAD_AGGRESSIVENESS` を上げます。`0` が最もゆるく、`3` が最も厳しい設定です。
STT の `STT_PROMPT` は候補を強く指定しすぎると、雑音でも候補文へ寄ることがあります。通常は中立的なプロンプトにしてください。
Wake 後の無音や短い雑音を発話として拾う場合は `VAD_START_FRAMES` や `MIN_SPEECH_SECONDS` を調整します。

起動:

```bash
cd /Users/shin/work/V_agent/voice-listener
.venv/bin/python voice_listener.py
```

使い方:

```text
ヘイ ケミー
洗面所の電気をつけて
```

音声認識に失敗したときは、最後の録音を再生してマイク入力を確認できます。

```bash
afplay /tmp/voice-listener-last.wav
```

ログの目安:

```text
rms=0.015 以上: 比較的良好
peak=0.7 未満: 音割れしにくい
peak=1.0 付近: MIC_GAIN を下げる
peak=0.05 未満: 小さすぎるため STT に送らない
```

VAD の目安:

```text
VAD_AGGRESSIVENESS=2: 標準〜やや厳しめ
VAD_AGGRESSIVENESS=3: 雑音に強いが、小さい声を落としやすい
VAD_START_FRAMES=4: 20ms frame が4回連続で音声になったら録音開始
```

Wake 後の雑音対策:

```text
VAD_START_FRAMES: 録音開始に必要な連続音声フレーム数
MIN_SPEECH_SECONDS: STT に送る最低音声判定時間
FALSE_WAKE_COOLDOWN_SECONDS: 雑音として弾いた後の短い待機時間
```

遅延計測:

```text
[timing] capture wake_to_record=0.00s wait_for_speech=0.28s speech_wall=2.10s record_wall=3.00s wav_write=0.01s
[timing] turn record=3.00s stt=1.20s openclaw=12.40s wake_to_openclaw_done=16.70s
```

主な見方:

```text
wait_for_speech: Wake word 後、発話開始と判定されるまでの時間
speech_wall: VAD上の発話区間の壁時計時間
record_wall: Wake後の録音全体。END_SILENCE_SECONDS が効く
stt: OpenAI STT API の時間
openclaw: interaction-bridge /send-text から OpenClaw 応答完了までの時間
wake_to_openclaw_done: Wake検出から OpenClaw 応答完了までの合計
```

STT 精度に影響するため、`get_audio_samples` は余った音声サンプルを捨てずに次回へ持ち越します。ここを壊すと録音が不自然になり、認識精度が大きく落ちます。

## 手動テスト

OpenClaw にテキストを送る:

```bash
curl -sS -X POST http://127.0.0.1:18089/send-text \
  -H 'Content-Type: application/json' \
  -d '{"text":"洗面所の電気をつけて"}'
```

AITuber Kit に直接発話させる:

```bash
curl -sS -X POST http://127.0.0.1:18089/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"こんにちは。お家のAIキャラクターです。","emotion":"happy"}'
```

許可済みの Home Assistant 操作を実行する:

```bash
curl -sS -X POST http://127.0.0.1:18088/run/bathroom_light_on
```

許可済みアクション一覧を見る:

```bash
curl -sS http://127.0.0.1:18088/actions
```

## Git 管理

このリポジトリは以下をルートとする monorepo として管理します。

```text
/Users/shin/work/V_agent
```

基本的に Git 操作は monorepo ルートで行います。

```bash
cd /Users/shin/work/V_agent
git status
git add .
git commit -m "変更内容"
```

もともと `openclaw` と `aituber-kit` は別々の Git リポジトリでしたが、現在は monorepo に統合済みです。元の `.git` は以下に退避され、Git 管理からは除外されています。

```text
openclaw/.git.backup-before-monorepo/
aituber-kit/.git.backup-before-monorepo/
```

## 秘密情報

`.env` ファイルは commit しないでください。

以下のローカル設定ファイルは `.gitignore` で除外されています。

```text
openclaw/.env
openclaw/ha-bridge/.env
aituber-kit/.env
voice-listener/.env
```

commit してよいのは `.env.example` のみです。

## メモ

- `openclaw/docker-compose.override.yml` では `OPENCLAW_AGENT_RUNTIME=pi` を設定しています。これは Docker 環境で利用できない `codex` harness を OpenClaw が選ばないようにするためです。
- 家電操作後の発話は `ha-bridge` 側で行います。OpenClaw のモデルがスキル指示を読み飛ばしても、操作成功後に確実に AITuber Kit へ発話できます。
- `node_modules`、`.venv`、`.next`、ログ、退避した古い `.git` は Git 管理対象外です。
