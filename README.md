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
claude calls + one tiny zcode call) that verifies both CLIs. After that the
plugin works in any repo on the device — repo sets are resolved per plan,
not per device (see [Multiple projects on one device](#multiple-projects-on-one-device)).

### ZCode headless config (handled automatically)

The headless CLI reads `~/.zcode/cli/config.json`, separate from the
desktop app's own config (known ZCode 0.16.x behavior), and refuses to run
until a model provider is declared there. Since 0.3.1 the preflight detects
that condition on a fresh device, merges the defaults below into the file
(never overwriting existing values, never storing secrets), and retries —
so first use needs no manual setup. The merged content is:

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

The engine locates the bundled CLI (`zcode.cjs`) automatically on Windows
(per-user and machine-wide installs), Linux (`/opt/ZCode`), and macOS
(`/Applications` and `~/Applications`); a `zcode` found on PATH is only a
last resort — on Linux that is the Electron desktop binary, which does not
serve headless prompts. Nonstandard install location? Pass
`--zcode-cli <path to zcode.cjs>`.

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
- `--iterations N` — raise the per-invocation review cap (default 4).
- `--repos A,B` — review against exactly these repos for this run. When the
  plan lives in one of them and the project isn't known yet, the set is
  remembered for that project (see below); on a known project it acts as a
  one-off override and nothing is rewritten.

### What happens when you invoke it

1. The session writes a **context brief** (intent, constraints, decisions
   already made in-chat, out of scope) next to the plan — this is how the
   reviewer inherits your conversation's intent. The revising claude gets
   the fuller picture by forking the conversation that invoked the run —
   automatic and derived, not a flag: the engine command embeds a nonce that
   identifies exactly which chat triggered it, so re-triggering from the
   same chat reuses its context, and starting a new chat is the one way to
   reset it. (Stale context can therefore only come from staying in an old
   chat — a user decision, by design.)
2. The engine builds a **synthetic workspace** (`~/.countersign/ws/<hash>/`)
   containing links to exactly the repos resolved for this plan's project —
   by default the one repo the plan lives in. The agents see those and
   nothing else; repos from other projects are never linked or mentioned.
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
   Decisions are cumulative across rounds — earlier answers persist in the
   plan's history dir, so a later round's answers never erase them.
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

## Multiple projects on one device

Repo sets are per project, never per device. Resolution order:

1. **Default — zero configuration:** the workspace links exactly the git repo
   the plan file lives in. `/countersign docs/plans/foo.md` in any new repo
   works on first try; nothing from other projects is linked or mentioned.
2. **Multi-repo projects:** when a plan spans repos (e.g. a backend +
   frontend pair), declare the set once:

   ```
   /countersign plan.md --repos ~/Documents/ladderly_backend,~/Documents/ladderly_frontend
   ```

   The set is saved to `~/.countersign/config.json` and matched by
   membership: a plan written in either member repo resolves to the whole
   set, so backend plans see the frontend and vice versa. Keys are just
   labels; matching is on resolved repo paths:

   ```json
   {
     "projects": {
       "ladderly": {
         "repos": ["~/Documents/ladderly_backend", "~/Documents/ladderly_frontend"]
       }
     }
   }
   ```

   A `--repos` on a project that's already known is a deliberate one-off
   override (e.g. narrow a backend-only plan) and does not rewrite the saved
   entry.
3. **Safety net:** if the plan's own repository ends up absent from the
   resolved workspace (stale config, wrong paths), the run emits a loud
   warning that the chat surfaces verbatim. The old flat device-wide `repos`
   array (pre-0.4) is migrated into a project entry on first use — it applied
   one repo set to every plan on the device, which linked the wrong repos
   when switching projects.

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
| `tests/run_e2e.py` | End-to-end engine test with stub agents (zero model spend): `python3 tests/run_e2e.py` |

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
- A revision that comes back a fraction of the plan's size is treated as a
  truncated output turn, not a revision: the engine re-asks once, then stops
  with `revise-truncated` leaving the plan untouched at its last good state.
  (Each revise round re-emits the FULL document, so plans near/above ~100KB
  outgrow a single output turn — split them; the engine also warns up-front
  and scales the revise timeout with document size.)
