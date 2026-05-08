# V_agent

Local monorepo for the home AI character setup.

This repository combines OpenClaw, AITuber Kit, a Home Assistant bridge, an interaction bridge, and a small WebSocket bridge so a browser voice input can control the home and make the AI character speak.

## Overview

```text
Browser recording
  -> openclaw/interaction-bridge
  -> OpenClaw gateway
  -> openclaw/ha-bridge
  -> Home Assistant
  -> openclaw/interaction-bridge /speak
  -> ha-character-bridge WebSocket
  -> AITuber Kit
  -> VOICEVOX
```

## Directories

```text
.
├── openclaw/
│   ├── docker-compose.yml
│   ├── docker-compose.override.yml
│   ├── ha-bridge/
│   └── interaction-bridge/
├── aituber-kit/
├── ha-character-bridge/
├── README.md
└── AGENTS.md
```

### openclaw

Runs the OpenClaw gateway and local bridge services.

Important local additions:

- `openclaw/ha-bridge`: exposes approved Home Assistant actions.
- `openclaw/interaction-bridge`: accepts browser audio/text, forwards text to OpenClaw, and sends speech events to AITuber Kit.
- `openclaw/docker-compose.override.yml`: local Docker services and runtime settings.

### aituber-kit

The character UI. It receives external speech/linkage events and speaks through the configured voice engine.

Common URL:

```text
http://localhost:3000
```

### ha-character-bridge

Small WebSocket bridge between `interaction-bridge` and AITuber Kit.

Common URL:

```text
ws://127.0.0.1:8000/ws
```

## Services And URLs

| Service | Host URL | Docker/internal URL |
| --- | --- | --- |
| AITuber Kit | `http://localhost:3000` | n/a |
| OpenClaw gateway | `http://127.0.0.1:18789` | `http://openclaw-gateway:18789` |
| ha-bridge | `http://127.0.0.1:18088` | `http://ha-bridge:8088` |
| interaction-bridge | `http://127.0.0.1:18089` | `http://interaction-bridge:8090` |
| ha-character-bridge | `ws://127.0.0.1:8000/ws` | `ws://host.docker.internal:8000/ws` |
| VOICEVOX | `http://127.0.0.1:50021` | `http://host.docker.internal:50021` |

## Start

### 1. Start AITuber Kit

```bash
cd /Users/shin/work/V_agent/aituber-kit
npm run dev
```

Open:

```text
http://localhost:3000
```

### 2. Start the character WebSocket bridge

```bash
cd /Users/shin/work/V_agent/ha-character-bridge
.venv/bin/python bridge_server.py
```

### 3. Start VOICEVOX

Use the native VOICEVOX app or another process that exposes:

```text
http://127.0.0.1:50021
```

Docker services should access it through:

```text
http://host.docker.internal:50021
```

### 4. Start OpenClaw services

```bash
cd /Users/shin/work/V_agent/openclaw
docker compose up -d openclaw-gateway ha-bridge interaction-bridge
```

Check status:

```bash
docker compose ps
```

## Browser Voice Input

Open the interaction bridge page:

```text
http://127.0.0.1:18089
```

Use the recording buttons. The flow is:

1. Browser records audio.
2. `interaction-bridge` transcribes it.
3. The text is sent to OpenClaw.
4. OpenClaw chooses an approved Home Assistant action through `ha-bridge`.
5. `ha-bridge` calls Home Assistant.
6. On success, `ha-bridge` calls `/speak`.
7. AITuber Kit speaks the result.

## Manual Tests

Send text to OpenClaw:

```bash
curl -sS -X POST http://127.0.0.1:18089/send-text \
  -H 'Content-Type: application/json' \
  -d '{"text":"洗面所の電気をつけて"}'
```

Speak through AITuber Kit:

```bash
curl -sS -X POST http://127.0.0.1:18089/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"こんにちは。お家のAIキャラクターです。","emotion":"happy"}'
```

Run an approved Home Assistant action:

```bash
curl -sS -X POST http://127.0.0.1:18088/run/bathroom_light_on
```

List approved actions:

```bash
curl -sS http://127.0.0.1:18088/actions
```

## Git Management

This repository is managed as one monorepo from:

```text
/Users/shin/work/V_agent
```

Use Git only from the monorepo root unless you have a specific reason.

```bash
cd /Users/shin/work/V_agent
git status
git add .
git commit -m "Describe the change"
```

The original nested repositories were backed up and ignored:

```text
openclaw/.git.backup-before-monorepo/
aituber-kit/.git.backup-before-monorepo/
```

## Secrets

Do not commit `.env` files.

Ignored local files include:

```text
openclaw/.env
openclaw/ha-bridge/.env
aituber-kit/.env
```

Commit `.env.example` files only.

## Notes

- `openclaw/docker-compose.override.yml` pins `OPENCLAW_AGENT_RUNTIME=pi` so OpenClaw does not try to use an unavailable `codex` harness.
- `ha-bridge` performs AITuber speech after a successful home action. This is more reliable than relying only on model instructions inside an OpenClaw skill.
- `node_modules`, `.venv`, `.next`, logs, and old nested `.git` backups are ignored.
