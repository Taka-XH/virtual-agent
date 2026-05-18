import os

import requests
from loguru import logger

HA_BRIDGE_URL = os.getenv("HA_BRIDGE_URL", "http://127.0.0.1:18088")

# Claude / OpenAI function calling 用ツール定義
HA_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_ha_action",
            "description": (
                "Home Assistant のデバイスを操作する。"
                "利用可能なアクション: "
                "bathroom_light_on (洗面所の照明をつける)、"
                "bathroom_light_off (洗面所の照明を消す)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "実行するアクション名",
                        "enum": ["bathroom_light_on", "bathroom_light_off"],
                    }
                },
                "required": ["action"],
            },
        },
    }
]


def run_ha_action(action: str) -> str:
    """ha-bridge 経由で HA アクションを実行し、結果の説明文を返す。"""
    url = f"{HA_BRIDGE_URL}/run/{action}"
    try:
        resp = requests.post(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        description = data.get("description", action)
        logger.info(f"[ha] {action} → {description}")
        return f"成功: {description}"
    except requests.RequestException as e:
        logger.warning(f"[ha] {action} failed: {e}")
        return f"エラー: {action} の実行に失敗しました"


def fetch_ha_actions() -> list[str]:
    """ha-bridge から利用可能なアクション一覧を取得する。"""
    try:
        resp = requests.get(f"{HA_BRIDGE_URL}/actions", timeout=5)
        resp.raise_for_status()
        return list(resp.json().keys())
    except requests.RequestException as e:
        logger.warning(f"[ha] failed to fetch actions: {e}")
        return []
