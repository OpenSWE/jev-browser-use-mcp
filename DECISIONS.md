# Decisions

Append-only. Newest last.

---

## 2026-09-20 — Design settled by a 14-round grilling session

41 decisions, confirmed verbatim by the user before any code was written. The
load-bearing ones, with the reason each exists rather than the alternative:

- **Evidence, never a verdict.** `status == "done"` in jev means *the model
  chose `DONE`*, not that the task succeeded; jev's own README says a `DONE`
  choice still requires independent verification. Returning `{"status":"done"}`
  to another agent would launder a claim into a fact. Hence `agent_claims_done`,
  a `verified: false` field, and page evidence the caller judges for itself.
- **Three field names carry their own warnings**, because a name survives
  summarization and truncation where a description does not:
  `agent_claims_done`, `page_text_untrusted`, `run_browser_task_as_me`.
- **Never retry a browser mutation** (jev's `AGENTS.md`). Where a mutation may
  or may not have landed, the outcome is `ambiguous_mutation`, the session is
  dropped, and nothing is retried. Only model failures retry, because
  `model.py` is explicit that no action executed.
- **Two server registrations, mode fixed per process.** Not a preference:
  `browser_harness.helpers` binds `NAME`/`SOCK` at import, so one process
  cannot serve both browser modes.
- **Deadline checked between ticks only.** The only safe stopping point; a
  mutation must never be interrupted mid-flight.

---

## 2026-09-20 — Repo stays local until the checks are green

`git init` here, real commits as the work lands, tests green, then stop and
report. The remote is created by the user with one command at that point:
`gh repo create OpenSWE/jev-browser-use-mcp --public --source=. --push`.

Reason: nothing about the remote unblocks a single line of the build, and the
org should not hold the name before the thing exists. The committed history is
what ships, so commits are made properly as the work goes, not squashed at the
end.

Decided-by: advisor

---

## 2026-09-20 — Dependency pinned to a full 40-char sha over https

```
jev-ultrafast @ git+https://github.com/browser-use/jev-ultrafast@1231850a0bf1a0c0341fe408ef1668dbbfdfac46
```

- `jev-ultrafast` is **not on PyPI** (404) and the repo has **no tags**, so a
  sha is the only immutable handle. All 40 characters, not the short form.
- **https, never ssh.** The local clone's remote is
  `git@github.com:browser-use/jev-ultrafast.git`; copying that form into the
  dependency would make the package uninstallable for anyone without a GitHub
  key. The upstream repo is public and MIT, so https resolves for everyone.
- HEAD is pinned even though that commit is docs-only: it is the tree in the
  `.venv` this design was read from and the one it is tested against.
- `browser-harness==0.1.13` arrives transitively. It is **not** re-pinned here,
  and the `browser-harness[mcp]` extra is never added.

Decided-by: advisor

---

## 2026-09-20 — Assert all 13 state keys, not the 9 that get mutated

`Agent.state` has **13** keys (`agent.py:27-41`): `browser`, `goal`, `page`,
`decision`, `history`, `status`, `plan`, `plan_index`, `decisions`,
`text_calls`, `elapsed_ms`, `started_at`, `record`. Retasking mutates 9 of
them — `browser` and `record` stay, and `page`/`decision` self-heal inside
`predict` at `agent.py:70-76`.

The contract assertion nonetheless compares the **full 13-key set**. The drift
that silently breaks retasking is an *added* key — say a new counter beside
`decisions` — which would gate task two at `agent.py:75` while a 9-key check
saw nothing wrong.

`self.pending_text` is an instance attribute **outside** `state`
(`agent.py:18`) and must be cleared too; a dict key-set assertion can never
cover it.

The two gates that make retasking necessary at all, and so what the assertion
protects: `status in {"done","blocked"}` raises at `agent.py:73`, and
`len(decisions) >= MAX_STEPS * 2` raises at `agent.py:75`.

Decided-by: advisor

---

## 2026-09-20 — Not bundling browser-harness's MCP tools is the default, and the reason is broader than two tools

`mcp_server.py` ships as a single top-level module in the base
`browser-harness` wheel. Not bundling therefore *excludes* nothing — it simply
never imports that module and never adds the `[mcp]` extra.

The reason is wider than first framed. jev's `README.md:105` states: *"Model
output never becomes selectors, coordinates, shell commands, or executable
JavaScript."* Against that promise it is not only `browser_js` and
`browser_cdp` that break it — `browser_fill`, `browser_wait_for_element` and
`browser_upload_file` each take a model-authored **selector**, and
`browser_click` takes model-authored **coordinates**. Most of the 23 tools
break the promise, not two.

Anyone wanting raw browser control registers `browser-harness-mcp` separately
and deliberately.

Decided-by: advisor

---

## 2026-09-20 — Offline tests need no API keys, but `TYPESAFE_API_KEY` raises a bare `KeyError`

Both keys are read lazily inside functions, not at import: `TYPESAFE_API_KEY`
at `model.py:119`, `TEXT_MODEL_API_KEY` at `model.py:161`. Any test that never
reaches `choose()` or `field_text()` needs no key and makes no network call.

But `model.py:119` is a bare `os.environ[...]` lookup, so a missing key
surfaces as a raw `KeyError` — caught at the tool boundary and returned as a
clean error. `model.py:163` already does this properly for the other key.

Decided-by: advisor
