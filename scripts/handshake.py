#!/usr/bin/env python3
"""Check the PUBLISHED package is installable and speaks MCP. Free -- no keys, no browser.

    python3 scripts/handshake.py [git+https://github.com/OpenSWE/jev-browser-use-mcp]

Complements scripts/smoke.py, which costs real API calls. This one only does
initialize + tools/list, so nothing ever reaches a model or a page. Dummy keys are
enough to clear check_env(); no tool is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SOURCE = "git+https://github.com/OpenSWE/jev-browser-use-mcp"


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else SOURCE
    env = {
        **os.environ,
        "TYPESAFE_API_KEY": "dummy",
        "TEXT_MODEL_API_KEY": "dummy",
        "TEXT_MODEL_BASE_URL": "https://example.invalid/v1",
        "TEXT_MODEL": "dummy/model",
    }
    env.pop("JEV_MCP_BROWSER", None)  # Probe the headless tool names, never the _as_me ones.

    proc = subprocess.Popen(
        ["uvx", "--from", source, "jev-browser-use-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    def send(**obj) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
        proc.stdin.flush()

    send(
        id=1,
        method="initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "handshake", "version": "0"},
        },
    )
    info = json.loads(proc.stdout.readline())["result"]["serverInfo"]
    send(method="notifications/initialized", params={})
    send(id=2, method="tools/list", params={})
    tools = json.loads(proc.stdout.readline())["result"]["tools"]
    proc.stdin.close()  # EOF is how a client stops a stdio server; signals are the fallback.
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    print(f"serverInfo: {info}")
    for tool in tools:
        schema = tool.get("inputSchema", {})
        print(f"  {tool['name']}: params={list(schema.get('properties', {}))} required={schema.get('required', [])}")

    names = {t["name"] for t in tools}
    if names != {"run_browser_task", "close_browser_session"}:
        print(f"FAIL: unexpected tools {sorted(names)}", file=sys.stderr)
        return 1
    if not info.get("version"):
        print("FAIL: serverInfo.version is empty", file=sys.stderr)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
