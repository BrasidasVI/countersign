# Dual-Agent Orchestrator

A scripted consensus loop between two AI coding agents: **Claude Code drafts the
plan, ZCode (GLM) reviews it**, and the loop runs until the reviewer approves —
with product and company-direction decisions escalated to you, the human, along
the way.

```
seed idea ──> claude drafts plan ──> zcode reviews (3 channels)
                                        │
              ┌─────────────────────────┼──────────────────────┐
              ▼                         ▼                      ▼
        objections (technical)   open_questions (product)   fyi_notes (awareness)
        drafter revises          YOU decide, loop resumes   logged, never blocks
                                        │
                              approve + no questions = consensus
                                        │
                          optional implement pass (branch-guarded)
```

## What's in the box

| File | Purpose |
|---|---|
| `orchestrator.py` | The loop itself (Python 3.9+, stdlib only). Full design docs in its docstring. |
| `launch.sh` | Interactive launcher: device setup, per-run config, task prompt, auto-resume after human decisions. |
| `install.sh` | Installs the global `countersign` command on the device (Windows + Linux/macOS). |
| `agent-review-rules.md` | Review invariants (Ladderly-specific; edit to taste). Merges additively over built-in defaults. |
| `examples/` | Sample plans used to validate the review channels live. |

## Setup on a new device (Windows with Git Bash, or Linux)

1. **Clone this repo.**
2. **Python 3.9+** and **node** on PATH.
3. **Prerequisites** — verified automatically on first run, with install
   instructions in the error if missing:
   - **Claude Code** installed and logged in (`claude` on PATH): https://claude.ai/code
   - **ZCode** desktop app installed and logged in once (its bundled CLI is
     what gets invoked): https://z.ai
4. **ZCode headless config** (one-time quirk): the headless CLI reads
   `~/.zcode/cli/config.json`, separate from the desktop app's own config
   (known ZCode 0.16.3 behavior). Minimal working example:

   ```json
   {
     "provider": {
       "zai-coding-plan": {
         "kind": "anthropic",
         "options": { "baseURL": "https://api.z.ai/api/anthropic" }
       }
     },
     "model": { "main": "zai-coding-plan/GLM-5.3" }
   }
   ```

   Note `model.main` must be the STRING `"provider/model"`. The API key is read
   from the `ZCODE_API_KEY` environment variable, or auto-loaded from the ZCode
   desktop app's config (`~/.zcode/v2/config.json`) if you're logged in there.
5. **Install the command** (once per device, idempotent):

   ```bash
   ./install.sh
   ```

   This installs `countersign` (bash) into `~/.local/bin` — plus
   `countersign.cmd` on Windows so it also works in PowerShell/cmd — pointing
   at this clone. PATH is set up for you (with instructions if the automatic
   step is unavailable). Re-run it if you move or re-clone the repo.
   Uninstall = delete the shim file(s).

6. Run **`countersign` from any directory**. First run asks for your
   backend/frontend repo paths (saved to `device-config.sh`, gitignored), then
   verifies both CLIs via `--preflight` (run once per device, in an isolated
   temp directory). After that: confirm the task understanding, answer config
   prompts, and the loop runs.

## Using it

`./launch.sh` → answer config prompts (Enter accepts defaults) → type the task →
blank line to launch. Everything else is automatic:

- **Exit 0** — consensus. Plan at `<workspace>/plan.md`, audit trail in
  `<workspace>/plan-history/`.
- **Exit 3** — no consensus after max iterations; remaining objections are in
  `plan-history/review-iter-XX.json`.
- **Exit 4** — blocked on YOUR product decisions. The launcher shows you
  `open-questions.json`, waits for you to fill in `answer` fields, and re-runs
  automatically (settled decisions are authoritative; agents cannot re-litigate).
- **stdout is one JSON line** (logs go to stderr) — CI-gateable if you ever wire
  this into a pipeline.

### Multi-repo workspace

The loop runs against a **workspace directory containing both repos**, so the
drafter and reviewer see the full system (e.g. API routes *and* frontend types).
When you enable the implement pass, the launcher asks which repo it targets;
the never-on-main/master branch guard applies to that repo.

### Coding-plan usage limits (5h / weekly windows)

Neither provider exposes remaining quota headless, so the orchestrator estimates
before the run (logged), counts exact per-agent tokens after (in the stdout JSON
and `run-summary.json`), and detects quota errors by both providers' real error
strings — retrying short 429s with backoff and exiting `rate-limited` honestly
when a plan window is spent. The `--implement` pass is the expensive one; prefer
reaching consensus first and implementing when the window is fresh.

## Direct usage (without the launcher)

```bash
python orchestrator.py --preflight x --repo <workspace>     # verify both CLIs once
python orchestrator.py "your idea" --repo <workspace> \
    --review-rules agent-review-rules.md --max-iterations 4
```

`python orchestrator.py --help` for everything: session strategy (fresh vs
chained), decisions files, non-interactive mode, retries, timeouts, custom rules.

## Design notes

The full architecture — three-channel verdicts (objections / open_questions /
fyi_notes), human resolution semantics, invariants, CI output contract, rate-limit
handling, and the v2 post-implementation-review design — is documented in
`orchestrator.py`'s module docstring, which is the canonical spec.
