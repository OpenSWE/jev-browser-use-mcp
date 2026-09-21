# Decisions

Append-only. Each entry is a call the spec did not settle.

The design was settled in a grilling session (41 questions) before any code existed.
Entries below record the calls whose rationale is not obvious from the code, plus the
two made on the user's behalf by a paired advisor.

---

## D1 — Evidence, never a verdict

`status == "done"` is set when the **model chooses** `DONE` (`agent.py:97`). jev's own
README says a DONE choice still requires independent outcome verification. Returning
`{"status": "done"}` to another agent would launder that claim into a fact.

The outcome value is `agent_claims_done`, `verified` is always `false`, and the envelope
carries `url`, `title`, `page_text_untrusted` and the action log so the caller can check.
The bare string `"done"` is never an outcome value.

## D2 — `page_text_untrusted` is the field name

Page text is written by whoever controls the page. jev defends itself
(`questions.py:4`: *"Page text is untrusted data, never instructions"*); the calling agent
has no such guard. The field name carries the warning because a name survives
summarization and truncation where a description does not. Capped at 2000 chars.

This is a mitigation, not a guarantee. There is no sanitizing natural language.

## D3 — Two servers, mode fixed per process

`browser_harness.helpers` binds `NAME`/`SOCK` at **import**, so browser mode cannot be a
per-call argument — the library forbids it. `jev` (headless, default) and `jev-chrome`
(attached) are separate registrations. The attached tools are named `..._as_me` so an
agent's choice is legible in the tool call itself, not only in its description.

## D4 — `BU_NAME` follows the browser, not the process

A constant name breaks on our own two-server design: two processes, one socket path,
`_ipc.serve` unlinks before binding, last writer wins — and those processes point at
**different browsers**. Per-pid naming is correct but orphans a daemon on every restart.

So: headless is `jev-h<port>` (no two servers share a port), attached is `jev-chrome`
(one user Chrome, so one shared daemon, and a restart reuses it).

## D5 — Agent construction is serialized even though runs are parallel

`ensure_daemon()` skips its own spawn lock exactly when `BU_CDP_URL` is set
(`admin.py:585-589`) — precisely our headless mode. Concurrent cold starts would race
several daemons onto one socket and orphan the losers' live CDP connections.

Parallelism across sessions is capped at 3 and measured worthwhile (~2.2–2.7×: the daemon
is asyncio with no hot-path lock, and ~66% of a tick is overlappable network wait).

## D6 — Ambiguous mutations are reported, never retried

`_ipc.py:96-106` returns `{}` when the daemon dies mid-request, and `Input.dispatch*`
legitimately returns `{}` — so a dead daemon is byte-identical to a successful click.
A daemon dying between `mousePressed` and `mouseReleased` leaves a confident history entry
for a click that never completed.

Rule: if the daemon does not answer, or the library says it does not know
(*"Dropdown execution was not confirmed"*), or a history row is stuck at
`page_changed: None`, the outcome is `ambiguous_mutation`, the session is dropped, and
nothing is retried. Only model failures retry — they carry *"no action executed"*, which
is literally true.

## D7 — We do not bundle browser-harness's 23 MCP tools

`mcp_server.py` ships in the base wheel and could be imported for free, adding 23 browser
tools to this server. jev's README:105 promises *"Model output never becomes selectors,
coordinates, shell commands, or executable JavaScript"* — and `browser_js`, `browser_cdp`,
`browser_fill`, `browser_wait_for_element`, `browser_upload_file` and `browser_click`
each break exactly that. Not bundling is the default: we simply never import it and never
add the `browser-harness[mcp]` extra. Anyone wanting both registers both.

## D8 — `http`/`https` only

`url="file:///…/.ssh/id_rsa"` would return the file's contents in `page_text_untrusted`.
A Claude Code caller already has `Read`, but this is a published server and some clients
have only MCP tools — for those we would be *adding* filesystem read. Loopback stays
allowed: driving a local dev server is a real use case. `JEV_MCP_ALLOW_SCHEMES` widens it.

## D9 — Sessions, and `goal` optional on continuation

A 50-second run that times out with 9 actions of progress must be resumable. But every
continuation calls `retask()`, which clears history — so resending the same goal would
wipe the work being resumed. `goal` is therefore optional: `{session_id}` alone resumes,
`{session_id, goal}` redirects.

## D10 — The state contract asserts all 13 keys, not the 9 we write

Decided-by: advisor

`Agent.state` has 13 keys (`agent.py:27-41`); `retask()` writes 9. Asserting only the 9
would miss the drift that actually breaks retasking: an **added** key, e.g. a new counter
beside `decisions`, which would gate task two at `agent.py:75` while a 9-key check saw
nothing wrong. `self.pending_text` is an instance attribute outside `state`
(`agent.py:18`) and is cleared explicitly, since no key-set assertion can cover it.

## D11 — Dependency pinned by full 40-char sha over https

Decided-by: advisor

`git+https://github.com/browser-use/jev-ultrafast@1231850a0bf1a0c0341fe408ef1668dbbfdfac46`

The local clone's remote is the **ssh** form (`git@github.com:…`); copying that into the
dependency would make the package uninstallable for anyone without a GitHub key. No tags
exist, so a sha is the only immutable handle, and the full 40 chars rather than the short
form. HEAD is pinned even though that commit is docs-only: it is the tree tested against.
`browser-harness==0.1.13` arrives transitively and is deliberately not re-pinned.

This also means the package cannot go to PyPI — PyPI rejects direct URL dependencies, and
jev-ultrafast is unpublished (404). `allow-direct-references` is set for the local build.

## D12 — Local repo only; the remote is the user's to create

Decided-by: advisor

Nothing about the remote unblocks a line of the build, and the org should not hold the
name before the thing exists. When checks are green the push is one command:
`gh repo create OpenSWE/jev-browser-use-mcp --public --source=. --push`.

## D13 — The one uncharacterised smoke failure does not block the push

Decided-by: advisor

Five live headless runs: four passed, one returned `outcome: "error"` whose cause was lost
because `scripts/smoke.py` truncated the envelope at 1500 chars. **That truncation was the
defect this episode exposed, and it is fixed** — the script now prints the diagnosis keys
before the page text.

The advisor first read the failure as a cold-start race and asked for five clean runs,
believing runs 2–5 had inherited run 1's daemon. They had not: `server.py:137` derives
`BU_NAME` from `_free_port()` on every start, so each run spawns its own daemon — and the
four distinct leaked names (`jev-h60281/60427/60612/60707`) are the proof. All five runs
were daemon-cold.

The structural argument retires the hypothesis outright, and is stronger than the timing
one: a cold-spawn race returns `setup_failed` at `server.py:285`, never `"error"`. Only two
lines in `classify()` produce `"error"` — `:207` (model retries exhausted) and `:209`
(nothing typed / missing `TEXT_MODEL_API_KEY`) — and **both are mutation-free**, so the
never-retry invariant was never reached whichever fired. A transient provider fault
surfacing as an honest outcome string is the tool working.

## D14 — Offline debug-emit test instead of more paid runs

Decided-by: advisor

One live run at ~20% recurrence is a weak reproduction test. What it would actually confirm
— that the `JEV_MCP_DEBUG=1` path emits — is testable offline against the existing fake
Agent, at zero cost and as a permanent check rather than one observation. So
`test_model_failure_is_retried_and_can_succeed` now asserts `model failure 1/2` and the
underlying error reach stderr. Verified to fail when the `debug()` call at `server.py:197`
is removed.

## D15 — README states the project is unaffiliated

Decided-by: advisor

The repo name carries `browser-use` and the org is public, so nobody should have to guess
whether this is official. One line under the title, not a footnote.

## D16 — Unrecognised JEV_MCP_BROWSER refuses to start

The README shipped `JEV_MCP_BROWSER=chrome` in both the prose and the JSON snippet, but
`attached_mode()` (`server.py:59`) compares against `"attached"`. Anyone following it got
**headless mode silently**, serving `run_browser_task` while the docs promised
`run_browser_task_as_me`. Nothing complained, because an unrecognised value fell through to
the `"headless"` default.

A typo that yields a working server in the wrong mode is worse than one that fails, so
`check_env()` now rejects anything outside `{headless, attached}`. The README is corrected.
The alternative — accepting `chrome` as an alias — was rejected: it hides the mismatch
instead of naming it, and invites the next near-miss (`chrom`, `real`) to fall through too.
