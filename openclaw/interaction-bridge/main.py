import os
import json
import uuid
import tempfile
import base64
import hashlib
import time
from pathlib import Path

import websockets
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-1")

OPENCLAW_WS = os.getenv("OPENCLAW_WS", "ws://openclaw-gateway:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
OPENCLAW_SESSION_KEY = os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main")
OPENCLAW_CONFIG_DIR = os.getenv("OPENCLAW_CONFIG_DIR", "/home/node/.openclaw")

AITUBER_WS = os.getenv("AITUBER_WS", "ws://host.docker.internal:8000/ws")
OPERATOR_SCOPES = [
    "operator.admin",
    "operator.read",
    "operator.write",
    "operator.approvals",
    "operator.pairing",
]

app = FastAPI(title="Interaction Bridge")

client = OpenAI(api_key=OPENAI_API_KEY)


class SpeakRequest(BaseModel):
    text: str
    emotion: str = "happy"


class SendTextRequest(BaseModel):
    text: str


@app.get("/health")
def health():
    return {
        "status": "ok",
        "openclaw_ws": OPENCLAW_WS,
        "session_key": OPENCLAW_SESSION_KEY,
        "aituber_ws": AITUBER_WS,
        "stt_model": STT_MODEL,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <title>OpenClaw Voice Bridge</title>
  <style>
    body { font-family: system-ui, sans-serif; padding: 32px; }
    button { font-size: 20px; padding: 12px 24px; margin-right: 12px; }
    pre { background: #f5f5f5; padding: 16px; white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>OpenClaw Voice Bridge</h1>
  <p>録音 → STT → OpenClawへ送信します。</p>
  <button id="start">録音開始</button>
  <button id="stop" disabled>録音停止して送信</button>
  <pre id="log"></pre>

<script>
let mediaRecorder;
let chunks = [];

const log = (msg) => {
  document.getElementById("log").textContent += msg + "\\n";
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
    const res = await fetch("/voice", { method: "POST", body: formData });
    const data = await res.json();
    log(JSON.stringify(data, null, 2));
  };

  mediaRecorder.start();
  log("Recording started.");
  document.getElementById("start").disabled = true;
  document.getElementById("stop").disabled = false;
};

document.getElementById("stop").onclick = () => {
  mediaRecorder.stop();
  log("Recording stopped.");
  document.getElementById("start").disabled = false;
  document.getElementById("stop").disabled = true;
};
</script>
</body>
</html>
"""


async def send_to_aituber(text: str, emotion: str = "happy"):
    payload = {
        "text": text,
        "role": "assistant",
        "emotion": emotion,
        "type": "message",
    }

    async with websockets.connect(AITUBER_WS) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))


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
    scopes: list[str],
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


async def send_to_openclaw(message: str):
    if not OPENCLAW_GATEWAY_TOKEN:
        raise RuntimeError("OPENCLAW_GATEWAY_TOKEN is not set")

    async with websockets.connect(OPENCLAW_WS) as ws:
        # pre-connect challenge
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
        client_id = "gateway-client"
        client_mode = "backend"
        platform = "linux"
        role = "operator"
        signed_at_ms = int(time.time() * 1000)
        identity = load_device_identity()
        device = None
        if identity:
            payload = build_device_auth_payload(
                device_id=identity["device_id"],
                client_id=client_id,
                client_mode=client_mode,
                role=role,
                scopes=OPERATOR_SCOPES,
                signed_at_ms=signed_at_ms,
                token=OPENCLAW_GATEWAY_TOKEN,
                nonce=nonce,
                platform=platform,
            )
            device = {
                "id": identity["device_id"],
                "publicKey": identity["public_key"],
                "signature": sign_device_payload(identity["private_key_pem"], payload),
                "signedAt": signed_at_ms,
                "nonce": nonce,
            }

        connect_frame = {
            "type": "req",
            "id": connect_id,
            "method": "connect",
            "params": {
                "minProtocol": 4,
                "maxProtocol": 4,
                "client": {
                    "id": client_id,
                    "version": "0.1.0",
                    "platform": platform,
                    "mode": client_mode
                },
                "role": role,
                "scopes": OPERATOR_SCOPES,
                "caps": [],
                "commands": [],
                "permissions": {},
                "auth": {"token": OPENCLAW_GATEWAY_TOKEN},
                "device": device,
                "locale": "ja-JP",
                "userAgent": "interaction-bridge/0.1.0"
            }
        }

        await ws.send(json.dumps(connect_frame))
        connect_res = json.loads(await ws.recv())

        if not connect_res.get("ok"):
            raise RuntimeError(f"OpenClaw connect failed: {connect_res}")

        send_id = str(uuid.uuid4())
        send_frame = {
            "type": "req",
            "id": send_id,
            "method": "chat.send",
            "params": {
                "sessionKey": OPENCLAW_SESSION_KEY,
                "message": message,
                "deliver": False,
                "idempotencyKey": str(uuid.uuid4())
            }
        }

        await ws.send(json.dumps(send_frame))

        while True:
            raw = await ws.recv()
            data = json.loads(raw)

            if data.get("type") == "res" and data.get("id") == send_id:
                if not data.get("ok"):
                    raise RuntimeError(f"OpenClaw chat.send failed: {data}")
                return data


@app.post("/speak")
async def speak(req: SpeakRequest):
    try:
        await send_to_aituber(req.text, req.emotion)
        return {"status": "ok", "text": req.text, "emotion": req.emotion}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/send-text")
async def send_text(req: SendTextRequest):
    try:
        result = await send_to_openclaw(req.text)
        return {
            "status": "ok",
            "text": req.text,
            "openclaw": result,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/voice")
async def voice(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=STT_MODEL,
                file=audio_file,
                language="ja"
            )

        text = transcription.text.strip()

        if not text:
            return {"status": "empty", "text": ""}

        result = await send_to_openclaw(text)

        return {
            "status": "ok",
            "text": text,
            "openclaw": result,
        }

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
