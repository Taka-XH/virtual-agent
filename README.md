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

`experiment/aituberkit-browser-mic` ブランチでは、AITuber Kit のブラウザ音声認識で得たテキストを既存 WebSocket 経由で `interaction-bridge` に転送する実験を行います。

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
`experiment/aituberkit-browser-mic` では逆方向も扱い、AITuber Kit が外部連携モードで送る `{ content, type: "chat" }` を `interaction-bridge /send-text` へ転送します。

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
3. 文字起こしされたテキストを OpenClaw に送信
4. OpenClaw がカジュアルトークか家電操作かを判断
5. カジュアルトークなら OpenClaw の応答を AITuber Kit へ発話
6. 家電操作なら `ha-bridge` 経由で許可済みの Home Assistant 操作を実行
7. `/speak` が呼ばれた場合は、その発話を優先して二重発話を避ける

## AITuber Kit ブラウザマイク入力

`experiment/aituberkit-browser-mic` では、AITuber Kit の画面上のマイク入力をそのまま OpenClaw への入力に使えます。AITuber Kit のブラウザ音声認識はテキスト化までを担当し、`ha-character-bridge` が WebSocket で受け取ったテキストを `interaction-bridge /send-text` に転送します。

```text
AITuber Kit ブラウザ音声認識
  -> 外部連携 WebSocket ws://127.0.0.1:8000/ws
  -> ha-character-bridge
  -> interaction-bridge /send-text
  -> OpenClaw / ha-bridge
  -> interaction-bridge /speak
  -> ha-character-bridge
  -> AITuber Kit が発話
```

設定:

```text
AITuber Kit:
  外部連携モード: ON
  音声認識モード: ブラウザ
  常時マイク入力: 必要に応じてON

ha-character-bridge:
  INTERACTION_BRIDGE_URL=http://127.0.0.1:18089
```

起動:

```bash
cd /Users/shin/work/V_agent_aituberkit-browser-mic/ha-character-bridge
python3 bridge_server.py
```

この方式では Python の `voice-listener` がマイクを直接掴まないため、PortAudio のデバイス選択や入力音量問題を避けられます。wake word は使わず、AITuber Kit 側のマイクボタンまたは常時マイク入力で待ち受けます。

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

## 現在の作業ブランチ

2026-05-10 時点の基準コミットは以下です。

```text
4555e6cc Prepare voice companion baseline
```

このコミットを起点に、現行の音声待受改善と OpenClaw Talk mode 検証を分けて進めます。

| パス | ブランチ | 目的 |
| --- | --- | --- |
| `/Users/shin/work/V_agent` | `main` | 安定ベース。共通README、現行構成、共有済みの基準点 |
| `/Users/shin/work/V_agent_voice-listener-metrics` | `experiment/voice-listener-metrics` | 現行 `voice-listener` の計測、遅延分析、録音/STT/VADのブラッシュアップ |
| `/Users/shin/work/V_agent_openclaw-talk-mode` | `experiment/openclaw-talk-mode` | OpenClaw Talk mode、`talk.speak`、realtime/agent-consult 経路の検証 |

作業前に、どの検証をするかでディレクトリを選びます。

```bash
cd /Users/shin/work/V_agent_voice-listener-metrics
git status
```

```bash
cd /Users/shin/work/V_agent_openclaw-talk-mode
git status
```

差分確認:

```bash
cd /Users/shin/work/V_agent
git diff main...experiment/voice-listener-metrics
git diff main...experiment/openclaw-talk-mode
```

リモートにも以下の3ブランチを push 済みです。

```text
main
experiment/voice-listener-metrics
experiment/openclaw-talk-mode
```

注意:

- `.env` と `.venv` は worktree ごとにはコピーしていません。
- 実行検証するときは、必要な worktree にだけ `.env` や仮想環境を用意します。
- 2つの実験ブランチを同時に混ぜず、良さそうな変更だけ `main` に取り込みます。

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
