# browser-agent

> **Hands & eyes on a real browser for AI agents — a perception-first local Playwright microservice.**

[中文](README.md)

## Why

- **Works without vision or computer-use.** The perception layer turns pages into structured text (accessibility tree, page summaries, form analysis) — no screenshot vision, no proprietary computer-use capability. Any model that can make an HTTP request or run a shell command gets full control of a real browser.
- **Even with computer-use, often you shouldn't.** Screenshot-plus-coordinate loops push a full-page image into context every step — slow and expensive. Text-based perception carries far more information per token, and semantic targets survive UI refactors that break pixel coordinates.
- **Terminal coding agents have no browser.** After changing frontend code, agents like Claude Code can only verify indirectly through test assertions. This service lets them open the real page and check SPA rendering, cookies, auth state, and JS errors — truth, not guesses.
- **Selectors are brittle.** Semantic `find_and_act` (describe the target in natural language, no selector) plus automatic fuzzy-text self-healing on click timeouts survives UI refactors.
- **Cold-starting a browser per test is slow.** The service stays resident: one Chromium instance shared end-to-end, each task in an isolated context, command batches queued, idle browser reclaimed automatically.
- **Login state is a hassle.** Named profiles persist auth (cookies + localStorage + validity rules) to disk — reused across restarts, isolated across accounts.

Design philosophy: **the tool perceives, the AI decides.**

## How it works

```
AI agent (Claude Code / Cursor / any program)
   │  JSON command batches (HTTP)
   ▼
BrowserAgentClient (CLI / Python API; auto-starts & health-checks the service)
   ▼
browser-agent service (resident aiohttp: resource preflight / batch queueing / idle reclaim)
   │  single Chromium instance, reused
   ├─ session A — isolated context (cookies / localStorage separated)
   ├─ session B — isolated context + auth profile (login state across restarts)
   └─ always-available primitives: click / fill / eval_js / screenshot
```

**Three-tier degradation** — fall back level by level, never a dead end:

```
Semantic (preferred): get_a11y_tree / find_and_act / form_analyze / detect_captcha
     ↓ on failure → ai_hint suggests the fallback
Structural (backup): get_interactive_elements / get_page_summary / extract
     ↓ on failure → ai_hint suggests the fallback
Precise primitives (last resort): click / fill / eval_js / screenshot (always available)
```

## Quick start

```bash
pip install .          # or: pip install git+https://github.com/Howie-Hoo/browser-agent
playwright install chromium

# start the service (client-managed, recommended)
browser-agent-client start --url=http://localhost:3000 --port=7788
```

**Python API:**

```python
from browser_agent.client import BrowserAgentClient

with BrowserAgentClient(base_url="http://localhost:3000") as client:
    report = client.run([
        {"action": "navigate", "url": "http://localhost:3000"},
        {"action": "detect_captcha"},                      # probe for captchas before login/submit
        {"action": "form_analyze"},                        # inspect form structure before filling
        {"action": "find_and_act", "description": "Sign in", "act": "click"},   # semantic click
        {"action": "watch_for_change", "timeout": 10000,   # server-side condition watch, no polling
         "conditions": [{"type": "url_contains", "value": "/console"}]},
        {"action": "get_a11y_tree"},                       # accessibility tree (best for SPAs)
    ])
    print(report["summary"])
```

**CLI:** `status | start | run <json> | check <type> | reset | shutdown`

Full command reference (40+ commands / 15 HTTP endpoints): [docs/command-reference.md](docs/command-reference.md).

## Core capabilities

| Capability | Notes |
| --- | --- |
| Three-tier perception | Semantic → structural → precise primitives, with suggested fallbacks on failure |
| One browser, many sessions | Single Chromium, N isolated contexts; serialized command batches (`BROWSER_AGENT_QUEUE_TIMEOUT` caps waiting); idle browser auto-reclaimed |
| Auth profiles | Named login state on disk (Playwright storageState + rule: `url_pattern` / `js` / `cookie_exists`); `ensure_auth` reuses when valid, re-runs your login commands when stale; credentials never leave files/logs |
| Resource preflight | Memory check (min of host and cgroup v2) before launch; refuses below `BROWSER_AGENT_MIN_FREE_MB` to protect low-spec machines |
| AI-friendly details | Always HTTP 200 with an `ok` field, automatic failure screenshots + possible_cause, page summary attached to navigations, clip screenshots to save tokens, stealth fingerprint evasion |
| Process cleanup | Idempotent cleanup script (`browser-agent-cleanup`); three-layer safety net via pytest conftest / Claude Code Stop hooks |

Profile format and the two-step integration are documented in [SKILL.md](SKILL.md) (in Chinese).

## Install (Claude Code)

**One-liner (recommended)**:

```text
/plugin install browser-agent --marketplace Howie-Hoo/browser-agent
```

Then install the runtime (no clone needed):

```bash
pip install git+https://github.com/Howie-Hoo/browser-agent && playwright install chromium
```

Telling your AI "install this skill: https://github.com/Howie-Hoo/browser-agent" works too — it follows the two steps above.

**Manual (other agents / no plugin)**: clone the repo, `pip install -e <repo path>`, and point your project's `CLAUDE.md` at the repo-root [SKILL.md](SKILL.md) (the operator manual for AI agents: degradation tiers, standard flows, session model, constraints).

## Configuration

Copy `config.example.yaml` to `browser_agent.yaml` at the repo root (or set `BROWSER_AGENT_CONFIG`). CLI flags take precedence.

| Env var | Default | Meaning |
| --- | --- | --- |
| `BROWSER_AGENT_MIN_FREE_MB` | 768 | Minimum free memory (MB) required to launch the browser |
| `BROWSER_AGENT_MAX_LOAD` | unchecked | 1-min loadavg ceiling; refuse to start above it |
| `BROWSER_AGENT_QUEUE_TIMEOUT` | 600 | Max seconds a command batch waits in queue (0 = unlimited) |
| `BROWSER_AGENT_IDLE_SHUTDOWN_SEC` | 900 | Close the browser after all sessions idle this long (0 = disable) |
| `BROWSER_AGENT_REPORT_DIR` | repo temp/ | Output directory for reports / screenshots |
| `BROWSER_AGENT_CONFIG` | — | Override config file path |

## License

[MIT](LICENSE)
