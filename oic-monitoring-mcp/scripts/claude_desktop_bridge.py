#!/usr/bin/env python3
"""Bridge a stdio MCP client (like Claude Desktop) to the OIC WebSocket MCP server.

This script is intentionally small and strict:
- reads JSON-RPC messages from stdin, one per line
- forwards each message to ws://127.0.0.1:8085/ws
- writes each server reply to stdout, again one JSON object per line

Use it as a local command in the Claude Desktop config:

{
  "mcpServers": {
    "oic": {
      "command": "python",
      "args": ["D:\\oic-monitoring-mcp\\scripts\\claude_desktop_bridge.py"]
    }
  }
}

The script reads MCP_WS_URL from the environment if you want a different
WebSocket endpoint, for example:
  set MCP_WS_URL=ws://127.0.0.1:8085/ws
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import websockets


WS_URL = os.environ.get("MCP_WS_URL", "ws://127.0.0.1:8085/ws")


async def bridge_stdio_to_ws() -> None:
    """Forward messages between stdin/stdout and the websocket server."""
    async with websockets.connect(WS_URL, subprotocols=["mcp"]) as ws:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":
                break

            line = line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                error = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Parse error",
                        "data": str(exc),
                    },
                }
                sys.stdout.write(json.dumps(error, separators=(",", ":")) + "\n")
                sys.stdout.flush()
                continue

            await ws.send(json.dumps(payload, separators=(",", ":")))
            response = await ws.recv()
            sys.stdout.write(response.rstrip() + "\n")
            sys.stdout.flush()


async def main() -> None:
    try:
        await bridge_stdio_to_ws()
    except KeyboardInterrupt:
        return
    except Exception as exc:  # pragma: no cover - CLI bridge error path
        error = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000,
                "message": "Bridge error",
                "data": str(exc),
            },
        }
        sys.stderr.write(json.dumps(error, separators=(",", ":")) + "\n")
        sys.stderr.flush()
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
