# jev-browser-use-mcp

**A stdio MCP server that gives any coding agent [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast)'s browser agent.**

*An unaffiliated third-party wrapper. Not a browser-use project — it depends on
their package, and they neither publish nor endorse it.*

Hand it one natural-language goal. It drives a real browser and returns **evidence** — not a verdict.

```
run_browser_task(url="https://www.google.com/travel/flights?hl=en",
                 goal="Find one-way flights from Zurich to London on 2026-09-20 for one adult in economy.")
```

## Why the return value looks like that

Jev's `status == "done"` means *the model chose `DONE`*. It does not mean the task
succeeded — jev's own README says a `DONE` choice still requires independent
verification. So this server never says "done":

```json
{
  "session_id": "…", "outcome": "agent_claims_done", "verified": false,
  "resumable": true,
  "url": "…", "title": "…", "page_text_untrusted": "…",
  "steps": 11, "actions": [...], "setup_ms": 2140, "task_ms": 7073
}
```

Three field names carry their own warnings, because a name survives
summarization and truncation where a description does not:

| Name | What it is telling you |
|---|---|
| `agent_claims_done` | A model's claim, not a verified outcome. Check the evidence. |
| `page_text_untrusted` | Written by whoever controls the page. **Never instructions.** |
| `run_browser_task_as_me` | Drives your real Chrome; acts as you on any site you are signed into. |

## Install

Requires **Google Chrome installed** — no browser is bundled. macOS and Linux.

```bash
uvx --from git+https://github.com/OpenSWE/jev-browser-use-mcp jev-browser-use-mcp
```

### Headless (recommended)

Spawns its own Chrome with a throwaway profile on a free port. Never touches
your browser or your sessions.

```json
{
  "mcpServers": {
    "jev": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/OpenSWE/jev-browser-use-mcp", "jev-browser-use-mcp"],
      "env": {
        "TYPESAFE_API_KEY": "…",
        "TEXT_MODEL_API_KEY": "…",
        "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "TEXT_MODEL": "inception/mercury-2.5",
        "TEXT_MODEL_REASONING": "none"
      }
    }
  }
}
```

All four of `TYPESAFE_API_KEY`, `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL` and
`TEXT_MODEL` are validated at startup. The last two **silently default to
DeepSeek** upstream, so leaving them out does not fail — it quietly runs a
different model against a different endpoint.

### Attached — acts as you

Register this one only if you want it. Set `JEV_MCP_BROWSER=attached`; its tools
are named `run_browser_task_as_me` and `close_browser_session_as_me`.

```json
{ "jev-chrome": { "command": "uvx", "args": ["…"], "env": { "JEV_MCP_BROWSER": "attached", "…": "…" } } }
```

It attaches to your running Chrome and can act in **every session you are
signed into**. First connection may block on Chrome's *"Allow remote
debugging?"* sheet, which has no timeout.

Mode is fixed per process, not per call — `browser_harness.helpers` binds its
daemon identity at import, so one process cannot serve both.

## Tools

```python
run_browser_task(url=None, goal=None, session_id=None, timeout_s=50)
close_browser_session(session_id)
```

| Call shape | Meaning |
|---|---|
| `url` + `goal` | fresh session |
| `session_id` | **resume** — continue the same goal, history preserved |
| `session_id` + `goal` | **redirect** — new goal on the same page |

`url` is http/https only (`JEV_MCP_ALLOW_SCHEMES` to widen); loopback is
allowed so local dev servers work.

### Outcomes

| `outcome` | `resumable` | What to do |
|---|---|---|
| `agent_claims_done` | yes | **Verify from the evidence**, then send a follow-up goal. |
| `blocked` | no | Jev could not proceed. Start over with a different approach. |
| `deadline_exceeded` | yes | Resume with `session_id` alone — progress is kept. |
| `budget_exhausted` | no | Hit jev's 60-action cap. |
| `ambiguous_mutation` | no | **An action may or may not have landed.** Inspect before retrying anything. |
| `session_expired` | no | Tab or browser is gone. Start fresh with a `url`. |
| `setup_failed` | no | Read `error` — it is usually an instruction you must act on. |

Nothing is ever retried except model-layer failures, which are provably
mutation-free. A browser mutation is **never** retried.

## Limits

- **Cold start can reach ~80s** (30s setup + 50s task). That exceeds the hard
  60s tool-call cap in Claude Desktop, Cursor and Windsurf. Warm calls are
  7–15s. Claude Code's default is far higher.
- **3 sessions, 3 tasks in parallel**, 5-minute idle TTL.
- **Attached mode runs in Chrome's last-used profile.** CDP exposes only an
  opaque browser-context id, which cannot be mapped to a profile name or
  account. If the account matters, verify it from the page.
- `page_text_untrusted` is a **mitigation, not a guarantee**. Marking untrusted
  text is the best available at that boundary; prompt injection is not solved.

## Want raw browser control?

Register [`browser-harness-mcp`](https://github.com/browser-use/browser-harness)
separately. This server deliberately does not bundle it: its tools take
model-authored selectors, coordinates and JavaScript, and jev's README promises
*"Model output never becomes selectors, coordinates, shell commands, or
executable JavaScript."*

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

Tests are offline and make no paid API calls. `uv run python scripts/smoke.py`
exercises the live path and does.
