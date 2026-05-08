# AGENTS.md

Instructions for AI agents and future maintainers working in this monorepo.

## Repository Shape

This is a monorepo rooted at:

```text
/Users/shin/work/V_agent
```

Do not treat `openclaw/` or `aituber-kit/` as independent Git repositories anymore. Their original `.git` directories were moved to backup folders and are ignored.

Work from the monorepo root for Git operations:

```bash
cd /Users/shin/work/V_agent
git status
```

## Main Components

- `openclaw/`: OpenClaw gateway plus local Docker services.
- `openclaw/ha-bridge/`: approved Home Assistant action bridge.
- `openclaw/interaction-bridge/`: browser voice/text bridge and AITuber speech endpoint.
- `aituber-kit/`: character UI and speech frontend.
- `ha-character-bridge/`: WebSocket bridge used by `interaction-bridge` to send messages to AITuber Kit.

## Safety Rules

- Never commit real `.env` files.
- Never print or expose API keys, Home Assistant tokens, OpenClaw gateway tokens, cookies, or private keys.
- Do not call arbitrary Home Assistant services. Use only actions listed by `ha-bridge` at `/actions`.
- Do not unlock doors, disable alarms, open garages, or change camera privacy settings unless the user explicitly adds a safe approved action for it.
- Do not remove `.git.backup-before-monorepo` folders unless the user explicitly asks.
- Do not run destructive Git commands such as `git reset --hard` unless explicitly requested.

## Local Secrets

Known local secret files are ignored:

```text
openclaw/.env
openclaw/ha-bridge/.env
aituber-kit/.env
```

Use `.env.example` files for documentation.

## Common Commands

Start AITuber Kit:

```bash
cd /Users/shin/work/V_agent/aituber-kit
npm run dev
```

Start the WebSocket bridge:

```bash
cd /Users/shin/work/V_agent/ha-character-bridge
.venv/bin/python bridge_server.py
```

Start OpenClaw services:

```bash
cd /Users/shin/work/V_agent/openclaw
docker compose up -d openclaw-gateway ha-bridge interaction-bridge
```

Check OpenClaw services:

```bash
cd /Users/shin/work/V_agent/openclaw
docker compose ps
docker compose logs --tail=120 openclaw-gateway interaction-bridge ha-bridge
```

## Expected Ports

- AITuber Kit: `http://localhost:3000`
- OpenClaw gateway: `http://127.0.0.1:18789`
- ha-bridge: `http://127.0.0.1:18088`
- interaction-bridge: `http://127.0.0.1:18089`
- ha-character-bridge: `ws://127.0.0.1:8000/ws`
- VOICEVOX host URL: `http://127.0.0.1:50021`
- VOICEVOX from Docker: `http://host.docker.internal:50021`

## Testing The Flow

Send text into OpenClaw:

```bash
curl -sS -X POST http://127.0.0.1:18089/send-text \
  -H 'Content-Type: application/json' \
  -d '{"text":"洗面所の電気をつけて"}'
```

Speak directly through AITuber Kit:

```bash
curl -sS -X POST http://127.0.0.1:18089/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"こんにちは。お家のAIキャラクターです。","emotion":"happy"}'
```

Run a Home Assistant action:

```bash
curl -sS -X POST http://127.0.0.1:18088/run/bathroom_light_on
```

The `ha-bridge` response should include a `speak` field with `status: ok` when AITuber speech succeeds.

## Implementation Notes

- `openclaw/docker-compose.override.yml` contains local integration settings.
- `OPENCLAW_AGENT_RUNTIME=pi` is intentional. It avoids the missing `codex` harness error in this Docker setup.
- `ha-bridge` calls `interaction-bridge /speak` after a successful Home Assistant action. This makes speech reliable even when the OpenClaw model does not follow the skill instruction to call `/speak`.
- `interaction-bridge` signs into OpenClaw as a paired device and requests operator scopes.
- Docker services reach the host through `host.docker.internal`.

## Editing Guidance

- Keep local integration changes small and explicit.
- Prefer editing `openclaw/ha-bridge/devices.yaml` when adding approved home actions.
- If adding a new home action, include:
  - `description`
  - `domain`
  - `service`
  - `entity_id`
  - `speak`
  - `emotion`
- If changing ports or URLs, update both `README.md` and this file.
- After changing Docker services, rebuild only the affected service when possible.

## Git Workflow

Use the monorepo root:

```bash
cd /Users/shin/work/V_agent
git status
git add .
git commit -m "Short description"
```

Before committing, check that ignored secrets are not staged:

```bash
git diff --cached --name-only | rg '(^|/)\\.env($|\\.)|node_modules|\\.venv|\\.git\\.backup|__pycache__|\\.next'
```

The command above should return no real secret or dependency files. `.env.example` files are acceptable.
