#!/usr/bin/env python3
"""Register Home Assistant's MCP server in the local OpenClaw config.

This script intentionally writes only to the user's local OpenClaw config. It
does not copy Home Assistant tokens into Git-tracked files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        default="openclaw/.env",
        help="Local env file containing HA_URL and HA_TOKEN",
    )
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".openclaw" / "openclaw.json"),
        help="OpenClaw config path to update",
    )
    parser.add_argument(
        "--name",
        default="home-assistant",
        help="MCP server name in OpenClaw config",
    )
    args = parser.parse_args()

    env = read_env_file(Path(args.env_file))
    ha_url = env.get("HOME_ASSISTANT_MCP_URL") or (
        env.get("HA_URL", "").rstrip("/") + "/api/mcp" if env.get("HA_URL") else ""
    )
    if not ha_url:
        raise SystemExit("HA_URL or HOME_ASSISTANT_MCP_URL is required in the env file")
    if not env.get("HA_TOKEN"):
        raise SystemExit("HA_TOKEN is required in the env file")

    config_path = Path(args.config).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = load_json(config_path)

    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("config.mcp must be an object")
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("config.mcp.servers must be an object")

    servers[args.name] = {
        "url": ha_url,
        "transport": "streamable-http",
        "connectionTimeoutMs": 10000,
        "headers": {
            "Authorization": "Bearer ${HA_TOKEN}",
        },
    }

    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"registered MCP server: {args.name}")
    print(f"config: {config_path}")
    print("token storage: HA_TOKEN environment variable reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
