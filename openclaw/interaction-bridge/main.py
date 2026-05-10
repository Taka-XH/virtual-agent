import os
import json
import uuid
import tempfile
import base64
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List

import websockets
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
    <p>録音 → STT → OpenClawへそのまま送信します。</p>
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

  log("Sending text to OpenClaw: " + text);

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


async def send_to_openclaw(message: str) -> Dict[str, Any]:
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

        return send_res


@app.post("/send-text")
async def send_text(req: SendTextRequest):
    """
    テキストを余計に加工せず、そのままOpenClawへ送る。
    会話の自然さ、記憶、人格、HA操作判断はOpenClaw側に任せる。
    """
    text = req.text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    try:
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
    音声をSTTし、認識結果をそのままOpenClawへ送る。
    ここでは独自の意図分類やプロンプト加工はしない。
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

        result = await send_to_openclaw(text)

        return {
            "status": "ok",
            "text": text,
            "openclaw": result,
        }

    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
