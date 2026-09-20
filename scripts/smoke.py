#!/usr/bin/env python3
"""Live smoke test. Run by hand, never in CI: it spends real API calls and needs Chrome.

    uv run --env-file .env python scripts/smoke.py

Headless by default. `JEV_MCP_BROWSER=attached` drives your real logged-in Chrome.
"""

from __future__ import annotations

import json
import sys

from jev_browser_use_mcp.server import RUNNER, check_env


def main() -> int:
    check_env()
    print(f"mode: {'attached' if RUNNER.attached else 'headless'}", file=sys.stderr)

    body = RUNNER.run_task("https://en.wikipedia.org/wiki/Main_Page", "search for Ada Lovelace", None, 50)
    print(json.dumps(body, indent=2)[:1500])

    if body["outcome"] not in {"agent_claims_done", "deadline_exceeded"}:
        print(f"\nFAIL: {body['outcome']}", file=sys.stderr)
        RUNNER.shutdown()
        return 1

    if body.get("session_id"):
        print("\n-- follow-up goal on the same tab --", file=sys.stderr)
        follow = RUNNER.run_task(None, "open the section about her notes on the Analytical Engine",
                                 body["session_id"], 50)
        print(json.dumps({k: follow.get(k) for k in ("outcome", "url", "steps", "task_ms")}, indent=2))
        RUNNER.close_session(body["session_id"])

    RUNNER.shutdown()
    print("\nOK -- outcome is the model's claim; verify the url and text above yourself.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
