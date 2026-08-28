# countersign

A Claude Code plugin that runs a two-agent consensus loop on a planning
document: the interactive Claude session (which holds your conversation
context) is the drafter of record; ZCode (GLM) reviews the plan headlessly;
a headless claude session applies each revision — back and forth, fully
self-driven, until the reviewer approves with zero outstanding objections.
Product decisions escalate to you
in the chat; nothing is ever committed or pushed automatically.

```
your planning doc (from the chat)
        │
        ▼
  /countersign docs/plans/foo.md
        │
        ▼
GLM (zcode headless) reviews ── objections ──> claude (headless) revises
        │                                            │
        │ open_questions (product/direction)         │ revised plan
        ▼                                            ▼
   YOU answer in chat  <── plugin mediates ──  loop until approve
        │
        ▼
consensus: final plan (+ optional branch-guarded implement pass)
```

## Install

Requires: Claude Code (claude CLI logged in), the ZCode desktop app
(its bundled headless CLI + a login), node, python 3.9+.

```bash
# from a Claude Code session:
/plugin marketplace add BrasidasVI/countersign
/plugin install countersign@countersign
```

(For local development, point the marketplace at your clone instead:
`/plugin marketplace add /path/to/countersign`.)

First use on a device: `/countersign` runs a one-time preflight (two tiny
claude calls + one tiny zcode call) that verifies both CLIs, then asks for
the repo paths involved (saved to `~/.countersign/config.json`).

### ZCode headless config (one-time quirk)

The headless CLI reads `~/.zcode/cli/config.json`, separate from the
desktop app's own config (known ZCode 0.16.3 behavior). Minimal working
example:

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

Note `model.main` must be the STRING `"provider/model"`. The API key comes
from the `ZCODE_API_KEY` environment variable, or auto-loads from the ZCode
desktop app's config (`~/.zcode/v2/config.json`) if you're logged in there.

## Use

Work on a plan in a normal Claude Code conversation — draft it, paste it,
or point at an existing doc. Then:

```
/countersign docs/plans/foo.md
```

Optional flags:

- `--implement` — after consensus, claude implements the plan (acceptEdits)
  in the repos the agents identified. Refuses to touch a repo on
  main/master; changes stay uncommitted.
- `--fork` — the revise calls fork THIS conversation for full context
  instead of working from the context brief. More capable, but it re-sends
  the whole conversation on every revise call — costly on 5h/weekly plan
  windows and rarely worth it for long chats. Default is a short
  context-brief file the session writes before the loop.
- `--iterations N` — raise the per-invocation review cap (default 4).

### What happens when you invoke it

1. The session writes a **context brief** (intent, constraints, decisions
   already made in-chat, out of scope) next to the plan — this is how the
   headless agents inherit your conversation cheaply.
2. The engine builds a **synthetic workspace** (`~/.countersign/ws/<hash>/`)
   containing links to exactly the repos you configured — both agents see
   those two repos and nothing else.
3. The loop runs, streaming progress (with a heartbeat) into the command
   output. Each iteration: GLM reviews → verdict parsed → blocking/minor
   objections (each with a concrete suggested fix) go to the revising
   claude → revised plan written in place (snapshots in
   `<plan-dir>/.countersign/<plan>-history/`). Consensus requires a review
   with zero objections of any severity: an approve-with-suggestions verdict
   gets one more revise round instead of ending the loop.
4. **Product/direction questions stop the loop** and appear in your chat
   with options and the reviewer's recommendation. Answer in plain text;
   the session records your decisions and resumes the loop automatically.
5. On consensus you get the final plan plus a summary of what changed and
   why, straight from the session that knows the intent.

### Usage limits (Claude Pro / z.ai 5h + weekly windows)

- A pre-run cost estimate is logged before the loop starts; per-agent token
  totals are reported in the final summary.
- Rate/quota hits back off exponentially; a spent window exits
  `rate-limited` with all state persisted (plan snapshots, reviews,
  sessions) — re-running later continues from the last iteration instead of
  re-spending the earlier ones.
- `--implement` is the budget-dominating pass; it is opt-in for that reason.

## Project review rules (per repo, versioned with the code)

If the repo containing the plan has `agent-review-rules.md` at its root,
those rules are added to the built-in invariants (test-coverage rationale,
no direct production rollout, no unverified repo assumptions) and
violations are always blocking. See the Ladderly backend repo for an
example.

## Repo layout

| Path | Purpose |
|---|---|
| `commands/countersign.md` | The `/countersign` command: instructions the interactive session follows. |
| `scripts/countersign_loop.py` | The engine: agent invocation, verdict parsing, retries/rate limits, implement passes. Full design doc in its docstring. |
| `tests/run_e2e.py` | End-to-end engine test with stub agents (zero model spend): `python tests/run_e2e.py` |

## Migrating from the terminal launcher (v0.1)

The `launch.sh` / `install.sh` terminal workflow is removed — the plugin is
the only interface now. If you installed the old global `countersign`
command, delete `~/.local/bin/countersign` and `countersign.cmd`. Existing
workspaces under `~/.countersign/ws/` are reused as-is by the plugin.

## Safety properties (unchanged from v0.1)

- Product/company-direction decisions always stop for the human.
- Implement passes refuse main/master BEFORE editing anything.
- Git commit and push are never automated.
- API keys are never written to logs, stdout, or files.
- Unparseable reviewer output is treated as a blocking objection, never approval.
