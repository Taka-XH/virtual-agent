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
- `voice-listener/`: local microphone wake-word listener that transcribes speech and sends text to `interaction-bridge`.

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
voice-listener/.env
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

Start the local wake-word voice listener:

```bash
cd /Users/shin/work/V_agent/voice-listener
.venv/bin/python voice_listener.py
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
- `interaction-bridge` can optionally orchestrate STT/text before OpenClaw when `ORCHESTRATION_ENABLED=true`.
- The orchestration fast path may call only action names returned by `ha-bridge /actions`; it must never construct arbitrary Home Assistant service calls.
- Obvious home actions can go directly to `ha-bridge`. Casual talk, ambiguous input, and low-confidence classifications must still go to OpenClaw.
- In the independent orchestration branch, keep `interaction-bridge` as the low-latency runtime. It may handle obvious home actions, lightweight replies, and memory-backed casual chat, but it should route requests that need OpenClaw memory, persona, skills, tools, or deeper reasoning back to OpenClaw.
- Home Assistant MCP is registered in local OpenClaw config as `mcp.servers.home-assistant` by `scripts/local/setup-homeassistant-mcp.py`. Keep the HA token as an environment variable reference; do not write token values into Git-tracked files.
- Home Assistant MCP requires the Home Assistant `Model Context Protocol Server` integration. If `/api/mcp` returns `404`, enable that integration and configure exposed entities before debugging OpenClaw.
- A small deterministic `local_quick_reply` layer is allowed for very common low-risk phrases so the companion still feels responsive when the LLM router is unavailable.
- A small deterministic `local_openclaw_required` layer should catch obvious memory/tool phrases before the LLM router, avoiding wasted router latency.
- A small deterministic `local_memory_chat_requested` layer should catch recent-history phrases such as "さっき" or "今の流れ" before the LLM router.
- The router may classify `needs_memory`, `needs_tools`, and `should_remember`. When routing to OpenClaw, it may tune the session through `sessions.patch` (`model`, `thinkingLevel`, `fastMode`, `reasoningLevel`) before `chat.send`.
- Local casual chat memory lives in SQLite at `MEMORY_DB_PATH`. Keep it lightweight: recent turns plus a compact `profile_summary`, not a full replacement for OpenClaw memory. Only update `profile_summary` when `should_remember=true`; short-term `memory_chat` alone should not create long-term memory.
- `OPENCLAW_SESSION_TUNING_ENABLED` must stay opt-in because `sessions.patch` persists session-level overrides.
- After `chat.send`, `interaction-bridge` waits for the OpenClaw run and reads the latest assistant message. If `/speak` was not called during the run, it sends that assistant text to AITuber Kit. If `/speak` was already called, it skips auto-speech to avoid duplicate character speech.
- `interaction-bridge` signs into OpenClaw as a paired device and requests operator scopes.
- Docker services reach the host through `host.docker.internal`.
- `voice-listener` uses `openwakeword` with the custom `Hey_Kemy` model at `voice-listener/models/Hey_Kemy.onnx` and OpenAI audio transcription. The current default STT model is `gpt-4o-transcribe`.
- `voice-listener` must preserve unused audio samples between reads. Dropping the remainder from a larger microphone chunk corrupts recordings and hurts STT accuracy.
- Failed or skipped voice recordings are saved to `/tmp/voice-listener-last.wav` when `LAST_WAV_PATH` is set.
- `MIC_GAIN` is a digital gain applied in `audio_callback`; watch `peak` in logs to avoid clipping.
- `VAD_AGGRESSIVENESS` controls WebRTC VAD strictness. Use `3` for noisy rooms and lower it if quiet speech is missed.
- Keep `STT_PROMPT` neutral. Strong candidate prompts can turn low-level noise into a plausible smart-home command.
- Quiet rejected recordings should not call `speak_status`; spoken error prompts can feed back into the microphone and trigger more false wakes.
- `VAD_START_FRAMES` and `MIN_SPEECH_SECONDS` are the main filters for post-wake noise being treated as a spoken command.
- `LOG_TIMINGS=true` makes `voice-listener` print per-turn latency for capture, STT, OpenClaw, and total wake-to-response timing. Keep this on while tuning latency.
- `interaction-bridge` returns `conversation.continue_listening=true` when the assistant asks a question or makes a proposal. `voice-listener` then waits for up to `FOLLOW_UP_RECORD_SECONDS` without requiring the wake word, capped by `FOLLOW_UP_MAX_TURNS`.

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
- If changing voice listener behavior or environment variables, update `voice-listener/.env.example` and `README.md`.
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
