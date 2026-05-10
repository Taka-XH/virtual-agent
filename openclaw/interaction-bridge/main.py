import os
import json
import uuid
import tempfile
import base64
import hashlib
import time
import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import websockets
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


# ============================================================
# Environment
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-1")

# Docker内のinteraction-bridgeからOpenClaw Gatewayへ接続するURL
OPENCLAW_WS = os.getenv("OPENCLAW_WS", "ws://openclaw-gateway:18789")

# OpenClaw Gateway token
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")

# 既定のOpenClawセッション
# 環境によって違う場合は .env で変更する
OPENCLAW_SESSION_KEY = os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main")
OPENCLAW_CONFIG_DIR = os.getenv("OPENCLAW_CONFIG_DIR", "/home/node/.openclaw")

# OpenClaw Gateway connect parameters
# 以前 operator.write 問題を直した設定に合わせて、必要なら .env で上書き可能
OPENCLAW_CLIENT_ID = os.getenv("OPENCLAW_CLIENT_ID", "gateway-client")
OPENCLAW_CLIENT_MODE = os.getenv("OPENCLAW_CLIENT_MODE", "backend")
OPENCLAW_CLIENT_PLATFORM = os.getenv("OPENCLAW_CLIENT_PLATFORM", "linux")
OPENCLAW_CLIENT_VERSION = os.getenv("OPENCLAW_CLIENT_VERSION", "0.1.0")
OPENCLAW_ROLE = os.getenv("OPENCLAW_ROLE", "operator")
OPENCLAW_SCOPES = [
    s.strip()
    for s in os.getenv(
        "OPENCLAW_SCOPES",
        "operator.admin,operator.read,operator.write,operator.approvals,operator.pairing",
    ).split(",")
    if s.strip()
]

# Docker内からMacホスト上のAITuberKitへ接続する場合
# AITuberKitも同じDocker networkなら ws://aituber-kit-app:8000/ws などに変更
AITUBER_WS = os.getenv("AITUBER_WS", "ws://host.docker.internal:8000/ws")

# OpenClaw応答待ち
OPENCLAW_TIMEOUT_SECONDS = float(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "60"))
OPENCLAW_REPLY_TIMEOUT_SECONDS = float(os.getenv("OPENCLAW_REPLY_TIMEOUT_SECONDS", "45"))

# Lightweight orchestration for the current voice-listener path.
# Obvious home actions can skip the full OpenClaw agent run.
ORCHESTRATION_ENABLED = os.getenv("ORCHESTRATION_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ORCHESTRATOR_LLM_ENABLED = os.getenv("ORCHESTRATOR_LLM_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gpt-4o-mini").strip()
ORCHESTRATOR_TIMEOUT_SECONDS = float(os.getenv("ORCHESTRATOR_TIMEOUT_SECONDS", "2.5"))
HA_BRIDGE_URL = os.getenv("HA_BRIDGE_URL", "http://ha-bridge:8088").rstrip("/")

# Direct casual talk path. This keeps lightweight conversation responsive while
# still routing deeper memory/tool/persona work to OpenClaw.
CASUAL_CHAT_ENABLED = os.getenv("CASUAL_CHAT_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CASUAL_CHAT_MODEL = os.getenv("CASUAL_CHAT_MODEL", "gpt-4o-mini").strip()
CASUAL_CHAT_TIMEOUT_SECONDS = float(os.getenv("CASUAL_CHAT_TIMEOUT_SECONDS", "8"))
CASUAL_CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CASUAL_CHAT_MAX_HISTORY_MESSAGES", "8"))
MEMORY_DB_PATH = os.getenv(
    "MEMORY_DB_PATH",
    str(Path(OPENCLAW_CONFIG_DIR) / "interaction-bridge-memory.sqlite3"),
)

# Optional OpenClaw session tuning. Disabled by default because sessions.patch
# persists model/thinking overrides for the OpenClaw session.
OPENCLAW_SESSION_TUNING_ENABLED = os.getenv(
    "OPENCLAW_SESSION_TUNING_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
OPENCLAW_FAST_MODEL = os.getenv("OPENCLAW_FAST_MODEL", "").strip()
OPENCLAW_BALANCED_MODEL = os.getenv("OPENCLAW_BALANCED_MODEL", "").strip()
OPENCLAW_DEEP_MODEL = os.getenv("OPENCLAW_DEEP_MODEL", "").strip()
OPENCLAW_DEEP_THINKING_LEVEL = os.getenv("OPENCLAW_DEEP_THINKING_LEVEL", "").strip()


# ============================================================
# App
# ============================================================

app = FastAPI(title="Interaction Bridge")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
last_speak_at_monotonic = 0.0


# ============================================================
# Models
# ============================================================

class SpeakRequest(BaseModel):
    text: str
    emotion: str = "happy"


class SendTextRequest(BaseModel):
    text: str


# ============================================================
# Health
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "openclaw_ws": OPENCLAW_WS,
        "openclaw_session_key": OPENCLAW_SESSION_KEY,
        "openclaw_client_id": OPENCLAW_CLIENT_ID,
        "openclaw_client_mode": OPENCLAW_CLIENT_MODE,
        "openclaw_role": OPENCLAW_ROLE,
        "openclaw_scopes": OPENCLAW_SCOPES,
        "aituber_ws": AITUBER_WS,
        "stt_model": STT_MODEL,
        "openai_api_key_configured": bool(OPENAI_API_KEY),
        "orchestration_enabled": ORCHESTRATION_ENABLED,
        "orchestrator_llm_enabled": ORCHESTRATOR_LLM_ENABLED,
        "orchestrator_model": ORCHESTRATOR_MODEL,
        "ha_bridge_url": HA_BRIDGE_URL,
        "casual_chat_enabled": CASUAL_CHAT_ENABLED,
        "casual_chat_model": CASUAL_CHAT_MODEL,
        "memory_db_path": MEMORY_DB_PATH,
        "openclaw_session_tuning_enabled": OPENCLAW_SESSION_TUNING_ENABLED,
    }


# ============================================================
# Simple browser recording UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <title>OpenClaw Voice Bridge</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      padding: 32px;
      line-height: 1.6;
    }
    button {
      font-size: 18px;
      padding: 12px 20px;
      margin-right: 12px;
      cursor: pointer;
    }
    input {
      font-size: 16px;
      padding: 10px;
      width: 520px;
      max-width: 90%;
    }
    pre {
      margin-top: 20px;
      background: #f5f5f5;
      padding: 16px;
      white-space: pre-wrap;
      border-radius: 8px;
    }
    .section {
      margin-bottom: 28px;
    }
  </style>
</head>
<body>
  <h1>OpenClaw Voice Bridge</h1>

  <div class="section">
    <h2>音声入力</h2>
    <p>録音 → STT → interaction-bridgeへ送信します。</p>
    <button id="start">録音開始</button>
    <button id="stop" disabled>録音停止して送信</button>
  </div>

  <div class="section">
    <h2>テキスト入力テスト</h2>
    <input id="textInput" placeholder="例：今日は少し疲れた" />
    <button id="sendText">OpenClawへ送信</button>
  </div>

  <div class="section">
    <h2>AITuberKit発話テスト</h2>
    <input id="speakInput" placeholder="例：こんにちは。ミカです。" />
    <button id="sendSpeak">キャラに喋らせる</button>
  </div>

  <pre id="log"></pre>

<script>
let mediaRecorder;
let chunks = [];

const log = (msg) => {
  const el = document.getElementById("log");
  el.textContent += msg + "\\n";
  el.scrollTop = el.scrollHeight;
};

document.getElementById("start").onclick = async () => {
  chunks = [];

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  mediaRecorder = new MediaRecorder(stream);

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };

  mediaRecorder.onstop = async () => {
    const blob = new Blob(chunks, { type: "audio/webm" });
    const formData = new FormData();
    formData.append("file", blob, "voice.webm");

    log("Uploading audio...");
    const res = await fetch("/voice", {
      method: "POST",
      body: formData
    });

    const data = await res.json();
    log(JSON.stringify(data, null, 2));
  };

  mediaRecorder.start();
  log("Recording started.");

  document.getElementById("start").disabled = true;
  document.getElementById("stop").disabled = false;
};

document.getElementById("stop").onclick = () => {
  if (mediaRecorder) {
    mediaRecorder.stop();
    log("Recording stopped.");
  }

  document.getElementById("start").disabled = false;
  document.getElementById("stop").disabled = true;
};

document.getElementById("sendText").onclick = async () => {
  const text = document.getElementById("textInput").value;
  if (!text.trim()) return;

  log("Sending text: " + text);

  const res = await fetch("/send-text", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ text })
  });

  const data = await res.json();
  log(JSON.stringify(data, null, 2));
};

document.getElementById("sendSpeak").onclick = async () => {
  const text = document.getElementById("speakInput").value;
  if (!text.trim()) return;

  log("Sending speak request: " + text);

  const res = await fetch("/speak", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      text,
      emotion: "happy"
    })
  });

  const data = await res.json();
  log(JSON.stringify(data, null, 2));
};
</script>
</body>
</html>
"""


# ============================================================
# AITuberKit
# ============================================================

async def send_to_aituber(text: str, emotion: str = "happy") -> None:
    """
    AITuberKit外部連携モードへ発話指示を送る。
    AITuberKit側では NEXT_PUBLIC_EXTERNAL_LINKAGE_MODE=true が必要。
    """
    if not text.strip():
        raise ValueError("text is empty")

    payload = {
        "text": text,
        "role": "assistant",
        "emotion": emotion,
        "type": "message",
    }

    async with websockets.connect(AITUBER_WS) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))


@app.post("/speak")
async def speak(req: SpeakRequest):
    global last_speak_at_monotonic

    text = req.text.strip()
    emotion = req.emotion.strip() or "happy"

    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    try:
        await send_to_aituber(text, emotion)
        last_speak_at_monotonic = time.monotonic()
        return {
            "status": "ok",
            "text": text,
            "emotion": emotion,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ============================================================
# Orchestration
# ============================================================

def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def memory_db_connect() -> sqlite3.Connection:
    path = Path(MEMORY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_memory_db() -> None:
    with memory_db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                route TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


@app.on_event("startup")
def startup() -> None:
    init_memory_db()


def append_memory_message(role: str, text: str, route: str) -> None:
    if not text.strip():
        return
    init_memory_db()
    with memory_db_connect() as conn:
        conn.execute(
            "INSERT INTO messages (created_at, role, text, route) VALUES (?, ?, ?, ?)",
            (time.time(), role, text.strip(), route),
        )


def load_recent_memory_messages(limit: int | None = None) -> List[Dict[str, str]]:
    init_memory_db()
    actual_limit = limit or CASUAL_CHAT_MAX_HISTORY_MESSAGES
    with memory_db_connect() as conn:
        rows = conn.execute(
            """
            SELECT role, text, route
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (actual_limit,),
        ).fetchall()
    return [
        {
            "role": str(row["role"]),
            "text": str(row["text"]),
            "route": str(row["route"]),
        }
        for row in reversed(rows)
    ]


def get_memory_value(key: str) -> str:
    init_memory_db()
    with memory_db_connect() as conn:
        row = conn.execute("SELECT value FROM memory WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_memory_value(key: str, value: str) -> None:
    init_memory_db()
    with memory_db_connect() as conn:
        conn.execute(
            """
            INSERT INTO memory (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value.strip(), time.time()),
        )


@app.get("/memory")
def memory_snapshot():
    return {
        "status": "ok",
        "profile_summary": get_memory_value("profile_summary"),
        "recent_messages": load_recent_memory_messages(),
    }


async def fetch_ha_actions() -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{HA_BRIDGE_URL}/actions")
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {}


async def run_ha_action(action_name: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{HA_BRIDGE_URL}/run/{action_name}")
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {"status": "ok"}


def normalize_japanese_command(text: str) -> str:
    return (
        text.strip()
        .replace("　", "")
        .replace(" ", "")
        .replace("ライト", "電気")
        .replace("照明", "電気")
    )


def local_home_action_match(text: str, actions: Dict[str, Any]) -> str | None:
    """
    Very small deterministic fast path for obvious home actions.
    Ambiguous requests fall through to the LLM/router or OpenClaw.
    """
    normalized = normalize_japanese_command(text)
    if not normalized:
        return None

    room_matches = {
        "bathroom": any(word in normalized for word in ("洗面所", "浴室", "お風呂", "風呂場")),
    }
    turn_on = any(word in normalized for word in ("つけて", "付けて", "点けて", "オン", "on"))
    turn_off = any(word in normalized for word in ("消して", "消す", "けして", "オフ", "off"))
    light = any(word in normalized for word in ("電気", "ライト", "照明"))

    if room_matches["bathroom"] and (light or "bathroom_light" in ",".join(actions.keys())):
        if turn_on and "bathroom_light_on" in actions:
            return "bathroom_light_on"
        if turn_off and "bathroom_light_off" in actions:
            return "bathroom_light_off"

    return None


def local_quick_reply(text: str) -> str | None:
    normalized = normalize_japanese_command(text)
    if not normalized:
        return None

    if any(word in normalized for word in ("ありがとう", "ありがと")):
        return "どういたしまして。いつでも声をかけてくださいね。"
    if any(word in normalized for word in ("ただいま", "帰ったよ", "かえったよ")):
        return "おかえりなさい。今日もおつかれさまでした。"
    if any(word in normalized for word in ("おはよう", "おはよ")):
        return "おはようございます。今日もゆっくり始めましょう。"
    if any(word in normalized for word in ("おやすみ", "寝るね", "ねるね")):
        return "おやすみなさい。ゆっくり休んでくださいね。"
    if any(word in normalized for word in ("疲れた", "つかれた", "励まして", "はげまして")):
        return "今日もよく頑張りました。少し肩の力を抜いて、ちゃんと休みましょう。"
    if any(word in normalized for word in ("短い一言", "ひとこと", "一言ください", "一言をください")):
        return "大丈夫。今のあなたのペースで、ちゃんと進めています。"

    return None


def local_openclaw_required(text: str) -> bool:
    normalized = normalize_japanese_command(text)
    if not normalized:
        return False

    memory_markers = (
        "前に話した",
        "以前話した",
        "覚えてる",
        "覚えている",
        "覚えておいて",
        "忘れないで",
        "私の好み",
        "いつもの",
    )
    tool_markers = (
        "調べて",
        "検索して",
        "予定",
        "カレンダー",
        "メール",
        "メモして",
    )
    return any(word in normalized for word in memory_markers + tool_markers)


def local_memory_chat_requested(text: str) -> bool:
    normalized = normalize_japanese_command(text)
    if not normalized:
        return False

    markers = (
        "さっき",
        "さきほど",
        "先ほど",
        "今の流れ",
        "会話の流れ",
        "最近の会話",
        "今話した",
        "直近",
    )
    return any(word in normalized for word in markers)


def parse_orchestrator_json(raw_text: str) -> Dict[str, Any] | None:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    return data if isinstance(data, dict) else None


async def classify_with_orchestrator_model(
    text: str,
    actions: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not ORCHESTRATOR_LLM_ENABLED or openai_client is None:
        return None

    allowed_actions = {
        name: {
            "description": action.get("description"),
            "domain": action.get("domain"),
            "service": action.get("service"),
            "entity_id": action.get("entity_id"),
        }
        for name, action in actions.items()
        if isinstance(action, dict)
    }
    prompt = (
        "あなたは家庭内AIの低遅延オーケストレーターです。"
        "ユーザー入力を実行経路に分類してください。"
        "許可済み家電操作に明確に一致する場合だけ home_action にしてください。"
        "履歴や記憶が不要な軽い返答は quick_reply にしてください。"
        "「さっき」「先ほど」「今の流れ」「最近の会話」「直近」など直前の会話を参照する自然会話は memory_chat にしてください。"
        "直近履歴や簡単なプロフィールで返せる自然会話も memory_chat にしてください。"
        "OpenClawのmemory、persona、skill、tool、深い推論、調査、永続記憶が必要なら openclaw にしてください。"
        "「覚えておいて」「忘れないで」など永続記憶の明示依頼は openclaw にしてください。"
        "quick_reply ではそのまま発話できる短い日本語 reply を入れてください。"
        "出力はJSONのみです。"
    )

    started_at = time.monotonic()
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai_client.chat.completions.create,
                model=ORCHESTRATOR_MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "text": text,
                                "allowed_actions": allowed_actions,
                                "schema": {
                                    "route": "home_action | quick_reply | memory_chat | openclaw | clarification",
                                    "action": "allowed action name or null",
                                    "confidence": "0.0-1.0",
                                    "reply": "quick_reply route only: short Japanese reply or null",
                                    "needs_memory": "boolean",
                                    "needs_tools": "boolean",
                                    "should_remember": "boolean",
                                    "openclaw_profile": "fast | balanced | deep",
                                    "reason": "short Japanese reason",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
            ),
            timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
        )
    except Exception as e:
        return {
            "route": "casual_talk",
            "action": None,
            "confidence": 0.0,
            "reason": f"orchestrator model failed: {e}",
            "elapsed_ms": elapsed_ms(started_at),
        }

    content = response.choices[0].message.content if response.choices else ""
    parsed = parse_orchestrator_json(content or "")
    if not parsed:
        return {
            "route": "casual_talk",
            "action": None,
            "confidence": 0.0,
            "reason": "orchestrator returned non-json",
            "elapsed_ms": elapsed_ms(started_at),
        }

    parsed["elapsed_ms"] = elapsed_ms(started_at)
    return parsed


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def generate_casual_reply(
    text: str,
    *,
    route: str,
    router: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if not CASUAL_CHAT_ENABLED or openai_client is None:
        raise RuntimeError("casual chat is disabled or OPENAI_API_KEY is not set")

    started_at = time.monotonic()
    profile_summary = get_memory_value("profile_summary")
    recent_messages = load_recent_memory_messages()
    system_prompt = (
        "あなたは家のAIキャラクターです。"
        "日本語で、親しみやすく、1〜2文で短く返答してください。"
        "家電操作、調査、予定管理、永続的に覚えるべき依頼は自分で実行したふりをしないでください。"
        "与えられたプロフィール要約と直近履歴は参考情報です。確信がない過去情報は断定しないでください。"
    )
    user_payload = {
        "user_text": text,
        "route": route,
        "router": router or {},
        "profile_summary": profile_summary,
        "recent_messages": recent_messages,
    }
    response = await asyncio.wait_for(
        asyncio.to_thread(
            openai_client.chat.completions.create,
            model=CASUAL_CHAT_MODEL,
            temperature=0.7,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            timeout=CASUAL_CHAT_TIMEOUT_SECONDS,
        ),
        timeout=CASUAL_CHAT_TIMEOUT_SECONDS,
    )
    reply = (response.choices[0].message.content if response.choices else "") or ""
    reply = reply.strip()
    if not reply:
        raise RuntimeError("casual chat returned empty reply")

    return {
        "reply": reply,
        "model": CASUAL_CHAT_MODEL,
        "profile_summary_used": bool(profile_summary),
        "history_messages_used": len(recent_messages),
        "elapsed_ms": elapsed_ms(started_at),
    }


async def update_profile_summary(text: str, reply: str, route: str) -> None:
    if openai_client is None:
        return

    current_summary = get_memory_value("profile_summary")
    prompt = (
        "あなたは家庭内AIの長期記憶メンテナンス係です。"
        "ユーザーの安定した好み、呼び方、生活上の重要情報、明示的に覚えてほしい内容だけを短く保存してください。"
        "一時的な雑談、単発の気分、家電操作結果は原則として保存しません。"
        "出力は日本語の箇条書きまたは空文字にしてください。"
    )
    payload = {
        "current_summary": current_summary,
        "new_exchange": {
            "route": route,
            "user": text,
            "assistant": reply,
        },
    }
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai_client.chat.completions.create,
                model=CASUAL_CHAT_MODEL,
                temperature=0,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                timeout=CASUAL_CHAT_TIMEOUT_SECONDS,
            ),
            timeout=CASUAL_CHAT_TIMEOUT_SECONDS,
        )
    except Exception:
        return

    summary = (response.choices[0].message.content if response.choices else "") or ""
    summary = summary.strip()
    if summary:
        set_memory_value("profile_summary", summary)


def schedule_memory_update(text: str, reply: str, route: str, router: Dict[str, Any] | None) -> None:
    should_remember = normalize_bool((router or {}).get("should_remember"))
    if should_remember:
        asyncio.create_task(update_profile_summary(text, reply, route))


async def finish_direct_reply(
    text: str,
    *,
    route: str,
    reply: str,
    router: Dict[str, Any] | None,
    source: str,
    started_at: float,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    speak_started_at = time.monotonic()
    await send_to_aituber(reply, "happy")
    append_memory_message("user", text, route)
    append_memory_message("assistant", reply, route)
    schedule_memory_update(text, reply, route, router)
    return {
        "status": "ok",
        "route": route,
        "text": text,
        "reply": reply,
        "orchestration": {
            "source": source,
            "router": router,
            **(extra or {}),
            "speak_elapsed_ms": elapsed_ms(speak_started_at),
            "total_elapsed_ms": elapsed_ms(started_at),
        },
    }


def resolve_openclaw_policy(router: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not OPENCLAW_SESSION_TUNING_ENABLED:
        return None

    profile = str((router or {}).get("openclaw_profile") or "balanced").strip().lower()
    if profile not in {"fast", "balanced", "deep"}:
        profile = "balanced"

    policy: Dict[str, Any] = {
        "profile": profile,
        "fastMode": profile == "fast",
        "reasoningLevel": "on" if profile == "deep" else "off",
    }

    if profile == "fast":
        policy["thinkingLevel"] = "off"
        if OPENCLAW_FAST_MODEL:
            policy["model"] = OPENCLAW_FAST_MODEL
    elif profile == "balanced":
        policy["thinkingLevel"] = "off"
        if OPENCLAW_BALANCED_MODEL:
            policy["model"] = OPENCLAW_BALANCED_MODEL
    else:
        if OPENCLAW_DEEP_THINKING_LEVEL:
            policy["thinkingLevel"] = OPENCLAW_DEEP_THINKING_LEVEL
        if OPENCLAW_DEEP_MODEL:
            policy["model"] = OPENCLAW_DEEP_MODEL

    return policy


async def orchestrate_text(text: str) -> Dict[str, Any]:
    started_at = time.monotonic()
    try:
        actions = await fetch_ha_actions()
    except Exception as e:
        openclaw_started_at = time.monotonic()
        openclaw_result = await send_to_openclaw(text)
        return {
            "status": "ok",
            "route": "openclaw",
            "text": text,
            "orchestration": {
                "source": "fallback",
                "error": f"failed to load ha actions: {e}",
                "openclaw_elapsed_ms": elapsed_ms(openclaw_started_at),
                "total_elapsed_ms": elapsed_ms(started_at),
            },
            "openclaw": openclaw_result,
        }

    actions_elapsed_ms = elapsed_ms(started_at)

    local_action = local_home_action_match(text, actions)
    if local_action:
        run_started_at = time.monotonic()
        ha_result = await run_ha_action(local_action)
        return {
            "status": "ok",
            "route": "home_action",
            "text": text,
            "orchestration": {
                "source": "local_rule",
                "action": local_action,
                "confidence": 1.0,
                "actions_elapsed_ms": actions_elapsed_ms,
                "ha_elapsed_ms": elapsed_ms(run_started_at),
                "total_elapsed_ms": elapsed_ms(started_at),
            },
            "ha_bridge": ha_result,
        }

    if local_openclaw_required(text):
        openclaw_started_at = time.monotonic()
        openclaw_result = await send_to_openclaw(text)
        return {
            "status": "ok",
            "route": "openclaw",
            "text": text,
            "orchestration": {
                "source": "local_openclaw_required",
                "actions_elapsed_ms": actions_elapsed_ms,
                "openclaw_elapsed_ms": elapsed_ms(openclaw_started_at),
                "total_elapsed_ms": elapsed_ms(started_at),
            },
            "openclaw": openclaw_result,
        }

    if local_memory_chat_requested(text):
        try:
            generated = await generate_casual_reply(
                text,
                route="memory_chat",
                router={
                    "route": "memory_chat",
                    "confidence": 1.0,
                    "reason": "直近の会話履歴を参照する表現をローカル判定",
                    "needs_memory": True,
                    "needs_tools": False,
                    "should_remember": False,
                    "openclaw_profile": "fast",
                },
            )
            return await finish_direct_reply(
                text,
                route="memory_chat",
                reply=generated["reply"],
                router={
                    "route": "memory_chat",
                    "confidence": 1.0,
                    "reason": "直近の会話履歴を参照する表現をローカル判定",
                    "needs_memory": True,
                    "needs_tools": False,
                    "should_remember": False,
                    "openclaw_profile": "fast",
                },
                source="local_memory_chat",
                started_at=started_at,
                extra={
                    "actions_elapsed_ms": actions_elapsed_ms,
                    "casual_chat": generated,
                },
            )
        except Exception as e:
            openclaw_started_at = time.monotonic()
            openclaw_result = await send_to_openclaw(text)
            return {
                "status": "ok",
                "route": "openclaw",
                "text": text,
                "orchestration": {
                    "source": "local_memory_chat_fallback",
                    "error": str(e),
                    "actions_elapsed_ms": actions_elapsed_ms,
                    "openclaw_elapsed_ms": elapsed_ms(openclaw_started_at),
                    "total_elapsed_ms": elapsed_ms(started_at),
                },
                "openclaw": openclaw_result,
            }

    quick_reply = local_quick_reply(text)
    if quick_reply:
        return await finish_direct_reply(
            text,
            route="quick_reply",
            reply=quick_reply,
            router=None,
            source="local_quick_reply",
            started_at=started_at,
            extra={"actions_elapsed_ms": actions_elapsed_ms},
        )

    model_route = await classify_with_orchestrator_model(text, actions)
    if model_route:
        route = str(model_route.get("route") or "casual_talk")
        if route == "casual_talk":
            route = "memory_chat"
        action = model_route.get("action")
        confidence = float(model_route.get("confidence") or 0.0)
        if (
            route == "home_action"
            and isinstance(action, str)
            and action in actions
            and confidence >= 0.75
        ):
            run_started_at = time.monotonic()
            ha_result = await run_ha_action(action)
            return {
                "status": "ok",
                "route": "home_action",
                "text": text,
                "orchestration": {
                    **model_route,
                    "source": "llm_router",
                    "actions_elapsed_ms": actions_elapsed_ms,
                    "ha_elapsed_ms": elapsed_ms(run_started_at),
                    "total_elapsed_ms": elapsed_ms(started_at),
                },
                "ha_bridge": ha_result,
            }

        if route == "quick_reply" and confidence >= 0.65:
            try:
                reply = str(model_route.get("reply") or "").strip()
                if not reply:
                    generated = await generate_casual_reply(text, route=route, router=model_route)
                    reply = generated["reply"]
                    return await finish_direct_reply(
                        text,
                        route=route,
                        reply=reply,
                        router=model_route,
                        source="llm_router",
                        started_at=started_at,
                        extra={
                            "actions_elapsed_ms": actions_elapsed_ms,
                            "casual_chat": generated,
                        },
                    )
                return await finish_direct_reply(
                    text,
                    route=route,
                    reply=reply,
                    router=model_route,
                    source="llm_router",
                    started_at=started_at,
                    extra={"actions_elapsed_ms": actions_elapsed_ms},
                )
            except Exception as e:
                model_route["direct_reply_error"] = str(e)

        if route == "memory_chat" and confidence >= 0.55:
            try:
                generated = await generate_casual_reply(text, route=route, router=model_route)
                return await finish_direct_reply(
                    text,
                    route=route,
                    reply=generated["reply"],
                    router=model_route,
                    source="llm_router",
                    started_at=started_at,
                    extra={
                        "actions_elapsed_ms": actions_elapsed_ms,
                        "casual_chat": generated,
                    },
                )
            except Exception as e:
                model_route["direct_reply_error"] = str(e)

    openclaw_started_at = time.monotonic()
    openclaw_policy = resolve_openclaw_policy(model_route)
    openclaw_result = await send_to_openclaw(text, policy=openclaw_policy)
    return {
        "status": "ok",
        "route": "openclaw",
        "text": text,
        "orchestration": {
            "source": "llm_router" if model_route else "fallback",
            "router": model_route,
            "openclaw_policy": openclaw_policy,
            "actions_elapsed_ms": actions_elapsed_ms,
            "openclaw_elapsed_ms": elapsed_ms(openclaw_started_at),
            "total_elapsed_ms": elapsed_ms(started_at),
        },
        "openclaw": openclaw_result,
    }


# ============================================================
# OpenClaw Gateway
# ============================================================

def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def load_device_identity() -> dict | None:
    path = Path(OPENCLAW_CONFIG_DIR) / "identity" / "device.json"
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as f:
        identity = json.load(f)

    if not all(
        isinstance(identity.get(key), str)
        for key in ("deviceId", "publicKeyPem", "privateKeyPem")
    ):
        return None

    public_key = serialization.load_pem_public_key(
        identity["publicKeyPem"].encode("utf-8")
    )
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        return None

    raw_public = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if hashlib.sha256(raw_public).hexdigest() != identity["deviceId"]:
        return None

    return {
        "device_id": identity["deviceId"],
        "public_key": base64url(raw_public),
        "private_key_pem": identity["privateKeyPem"],
    }


def normalize_device_metadata(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_device_auth_payload(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: List[str],
    signed_at_ms: int,
    token: str | None,
    nonce: str,
    platform: str,
    device_family: str | None = None,
) -> str:
    return "|".join(
        [
            "v3",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
            normalize_device_metadata(platform),
            normalize_device_metadata(device_family),
        ]
    )


def sign_device_payload(private_key_pem: str, payload: str) -> str:
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise RuntimeError("OpenClaw device private key is not Ed25519")
    return base64url(private_key.sign(payload.encode("utf-8")))


def build_connect_frame(connect_id: str, nonce: str) -> Dict[str, Any]:
    if not OPENCLAW_GATEWAY_TOKEN:
        raise RuntimeError("OPENCLAW_GATEWAY_TOKEN is not set")

    signed_at_ms = int(time.time() * 1000)
    identity = load_device_identity()
    device = None
    if identity:
        payload = build_device_auth_payload(
            device_id=identity["device_id"],
            client_id=OPENCLAW_CLIENT_ID,
            client_mode=OPENCLAW_CLIENT_MODE,
            role=OPENCLAW_ROLE,
            scopes=OPENCLAW_SCOPES,
            signed_at_ms=signed_at_ms,
            token=OPENCLAW_GATEWAY_TOKEN,
            nonce=nonce,
            platform=OPENCLAW_CLIENT_PLATFORM,
        )
        device = {
            "id": identity["device_id"],
            "publicKey": identity["public_key"],
            "signature": sign_device_payload(identity["private_key_pem"], payload),
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }

    return {
        "type": "req",
        "id": connect_id,
        "method": "connect",
        "params": {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": OPENCLAW_CLIENT_ID,
                "version": OPENCLAW_CLIENT_VERSION,
                "platform": OPENCLAW_CLIENT_PLATFORM,
                "mode": OPENCLAW_CLIENT_MODE,
            },
            "role": OPENCLAW_ROLE,
            "scopes": OPENCLAW_SCOPES,
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {
                "token": OPENCLAW_GATEWAY_TOKEN,
            },
            "device": device,
            "locale": "ja-JP",
            "userAgent": "interaction-bridge/0.1.0",
        },
    }


def build_chat_send_frame(send_id: str, message: str) -> Dict[str, Any]:
    return {
        "type": "req",
        "id": send_id,
        "method": "chat.send",
        "params": {
            "sessionKey": OPENCLAW_SESSION_KEY,
            "message": message,
            "deliver": False,
            "idempotencyKey": str(uuid.uuid4()),
        },
    }


async def wait_for_response(ws, request_id: str) -> Dict[str, Any]:
    """
    指定request idのresponseが返るまで待つ。
    途中でeventなどが流れてくることがあるため、該当idだけ拾う。
    """
    while True:
        raw = await ws.recv()
        data = json.loads(raw)

        if data.get("type") == "res" and data.get("id") == request_id:
            return data


async def request_gateway(ws, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    await ws.send(
        json.dumps(
            {
                "type": "req",
                "id": request_id,
                "method": method,
                "params": params,
            },
            ensure_ascii=False,
        )
    )
    res = await wait_for_response(ws, request_id)
    if not res.get("ok"):
        raise RuntimeError(f"OpenClaw {method} failed: {res}")
    payload = res.get("payload")
    return payload if isinstance(payload, dict) else {}


async def apply_openclaw_session_policy(ws, policy: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not policy:
        return None

    patch: Dict[str, Any] = {"key": OPENCLAW_SESSION_KEY}
    for key in ("model", "thinkingLevel", "fastMode", "reasoningLevel"):
        if key in policy and policy[key] not in ("", None):
            patch[key] = policy[key]

    if len(patch) == 1:
        return {"status": "skipped", "reason": "empty policy"}

    try:
        result = await request_gateway(ws, "sessions.patch", patch)
        return {
            "status": "ok",
            "patch": patch,
            "result": result,
        }
    except Exception as e:
        return {
            "status": "error",
            "patch": patch,
            "error": str(e),
        }


def extract_text_from_message(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    if message.get("role") != "assistant":
        return None

    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if not isinstance(content, list):
        return None

    parts: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            block_text = block.get("text")
            if isinstance(block_text, str) and block_text.strip():
                parts.append(block_text.strip())

    return "\n".join(parts).strip() or None


def extract_latest_assistant_text(messages: Any) -> str | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        text = extract_text_from_message(message)
        if text:
            return text
    return None


async def wait_for_assistant_text(ws, run_id: str, previous_assistant_text: str | None) -> str | None:
    try:
        await request_gateway(
            ws,
            "agent.wait",
            {
                "runId": run_id,
                "timeoutMs": int(OPENCLAW_REPLY_TIMEOUT_SECONDS * 1000),
            },
        )
    except Exception:
        # chat.history below is still useful if the run finished but wait failed
        pass

    history = await request_gateway(
        ws,
        "chat.history",
        {
            "sessionKey": OPENCLAW_SESSION_KEY,
            "limit": 16,
        },
    )
    text = extract_latest_assistant_text(history.get("messages"))
    if text and text != previous_assistant_text:
        return text
    return None


async def send_to_openclaw(message: str, policy: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    テキストをそのままOpenClawへ送る。
    ここではmemoryやpersonaのプロンプト加工はしない。
    """
    text = message.strip()

    if not text:
        raise ValueError("message is empty")

    run_started_at = time.monotonic()

    async with websockets.connect(
        OPENCLAW_WS,
        open_timeout=OPENCLAW_TIMEOUT_SECONDS,
        close_timeout=10,
    ) as ws:
        challenge_raw = await ws.recv()
        challenge = json.loads(challenge_raw)
        nonce = (
            challenge.get("payload", {}).get("nonce")
            if isinstance(challenge, dict)
            else None
        )
        if not nonce:
            raise RuntimeError("OpenClaw connect challenge missing nonce")

        connect_id = str(uuid.uuid4())
        connect_frame = build_connect_frame(connect_id, nonce)

        await ws.send(json.dumps(connect_frame, ensure_ascii=False))
        connect_res = await wait_for_response(ws, connect_id)

        if not connect_res.get("ok"):
            raise RuntimeError(f"OpenClaw connect failed: {connect_res}")

        openclaw_tuning_result = await apply_openclaw_session_policy(ws, policy)

        previous_history = await request_gateway(
            ws,
            "chat.history",
            {
                "sessionKey": OPENCLAW_SESSION_KEY,
                "limit": 16,
            },
        )
        previous_assistant_text = extract_latest_assistant_text(previous_history.get("messages"))

        send_id = str(uuid.uuid4())
        send_frame = build_chat_send_frame(send_id, text)

        await ws.send(json.dumps(send_frame, ensure_ascii=False))
        send_res = await wait_for_response(ws, send_id)

        if not send_res.get("ok"):
            raise RuntimeError(f"OpenClaw chat.send failed: {send_res}")

        payload = send_res.get("payload") if isinstance(send_res.get("payload"), dict) else {}
        run_id = payload.get("runId")
        assistant_text = None
        if isinstance(run_id, str) and run_id:
            assistant_text = await wait_for_assistant_text(ws, run_id, previous_assistant_text)
            if assistant_text:
                if last_speak_at_monotonic >= run_started_at:
                    send_res["assistant_text_spoken"] = False
                    send_res["speak_skipped"] = "speak endpoint was already called during this run"
                else:
                    try:
                        await send_to_aituber(assistant_text, "happy")
                        send_res["assistant_text_spoken"] = True
                    except Exception as e:
                        send_res["assistant_text_spoken"] = False
                        send_res["speak_error"] = str(e)

        if assistant_text:
            send_res["assistant_text"] = assistant_text

        if openclaw_tuning_result:
            send_res["openclaw_tuning"] = openclaw_tuning_result

        return send_res


@app.post("/send-text")
async def send_text(req: SendTextRequest):
    """
    通常はテキストをOpenClawへ送る。
    ORCHESTRATION_ENABLED=true の場合は、明確な家電操作だけfast pathで処理する。
    """
    text = req.text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    try:
        if ORCHESTRATION_ENABLED:
            return await orchestrate_text(text)

        result = await send_to_openclaw(text)
        return {
            "status": "ok",
            "text": text,
            "openclaw": result,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ============================================================
# STT
# ============================================================

def require_openai_client() -> OpenAI:
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return openai_client


@app.post("/voice")
async def voice(file: UploadFile = File(...)):
    """
    音声をSTTし、認識結果を /send-text と同じ経路で処理する。
    """
    client = require_openai_client()

    suffix = Path(file.filename or "audio.webm").suffix or ".webm"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=STT_MODEL,
                file=audio_file,
                language="ja",
            )

        text = transcription.text.strip()

        if not text:
            return {
                "status": "empty",
                "text": "",
            }

        if ORCHESTRATION_ENABLED:
            return await orchestrate_text(text)

        result = await send_to_openclaw(text)
        return {"status": "ok", "text": text, "openclaw": result}

    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
