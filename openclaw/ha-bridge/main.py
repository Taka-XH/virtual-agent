import os
from typing import Any, Dict

import requests
import yaml
from fastapi import FastAPI, HTTPException

HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
SPEAK_URL = os.getenv("SPEAK_URL", "http://interaction-bridge:8090/speak").strip()
SPEAK_ENABLED = os.getenv("SPEAK_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

if not HA_URL:
    raise RuntimeError("HA_URL is not set")

if not HA_TOKEN:
    raise RuntimeError("HA_TOKEN is not set")

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

app = FastAPI(title="Home Assistant Bridge")


def load_actions() -> Dict[str, Any]:
    with open("/app/devices.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("actions", {})


def speak_after_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not SPEAK_ENABLED or not SPEAK_URL:
        return {"status": "disabled"}

    text = action.get("speak") or action.get("description") or "操作しました。"
    emotion = action.get("emotion") or "happy"

    try:
        response = requests.post(
            SPEAK_URL,
            json={"text": text, "emotion": emotion},
            timeout=10,
        )
        response.raise_for_status()
        return {"status": "ok", "text": text, "emotion": emotion}
    except requests.RequestException as e:
        return {"status": "error", "detail": str(e)}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/actions")
def list_actions():
    actions = load_actions()
    return {
        name: {
            "description": action.get("description"),
            "domain": action.get("domain"),
            "service": action.get("service"),
            "entity_id": action.get("entity_id"),
        }
        for name, action in actions.items()
    }


@app.post("/run/{action_name}")
def run_action(action_name: str):
    actions = load_actions()

    if action_name not in actions:
        raise HTTPException(status_code=403, detail=f"Action not allowed: {action_name}")

    action = actions[action_name]

    domain = action["domain"]
    service = action["service"]
    entity_id = action["entity_id"]

    url = f"{HA_URL}/api/services/{domain}/{service}"
    payload = {"entity_id": entity_id}

    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "status": "ok",
        "action": action_name,
        "description": action.get("description"),
        "entity_id": entity_id,
        "speak": speak_after_action(action),
    }


@app.get("/state/{entity_id:path}")
def get_state(entity_id: str):
    url = f"{HA_URL}/api/states/{entity_id}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e))

    data = response.json()
    return {
        "entity_id": data.get("entity_id"),
        "state": data.get("state"),
        "attributes": data.get("attributes", {}),
    }
