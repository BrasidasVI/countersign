# countersign

A Claude Code plugin for two-agent consensus with a headless ZCode (GLM)
reviewer. Two entry points:

- **countersign skill — the default path.** When you ask your session to
  "implement <discussed change> using countersign", the session derives a
  short intent doc from the conversation, the consensus loop hardens it,
  the session implements on a feature branch, and a headless verify stage
  reviews the diff against the agreed intent, fixes blocking objections,
  and commits (branch-guarded). Chat to verified commit without leaving
  the conversation.
- **`/countersign` command — plan mode, opt-in.** Dual-agent consensus on
  a planning document BEFORE implementation: the interactive session
  (drafter of record) writes a plan; GLM reviews it headlessly; a headless
  claude session applies each revision — back and forth, fully self-driven,
  until the reviewer approves with zero outstanding objections. For
  design-heavy changes: architecture, data models, API contracts, security
  boundaries, migrations.

```
skill flow (default):

  chat discussion -> "implement using countersign"
        |
        v
  intent doc (derived from the chat, ~50 lines)
        |
        v
  consensus loop: GLM reviews <-> headless claude revises --> agreed intent
        |                     (product questions stop for YOU)
        v
  session implements on a feature branch
        |
        v
  verify stage: GLM reviews the diff AGAINST the intent
        |            blocking objections -> headless fixer -> re-review
        v
  engine commits (never on main/master); you push

plan mode (/countersign): your planning doc -> consensus loop -> final plan
```

In both entry points, product decisions escalate to you in the chat;
nothing is ever pushed.

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

**Implement flow (skill):** discuss a change in a normal session, then say
"implement this using countersign". The session writes the intent doc
(`.countersign/intent-<slug>.md` next to your work), runs the consensus
loop, implements on a feature branch, and runs verify — which reviews the
full working-tree diff (merge-base with the default branch through the
working tree, uncommitted and untracked changes included) against the
agreed intent. Blocking objections loop through a headless fixer (default
3 rounds); minors never block and are carried into the commit message. On
a clean review the ENGINE commits each touched repo — as your git
identity, no agent attribution, with a `countersign: verified` trailer.
Push stays yours.

**Plan mode (command):** work on a plan in a normal conversation — draft
it, paste it, or point at an existing doc. Then:

```
/countersign docs/plans/foo.md
```

Optional flags (plan mode; the skill needs none — the engine resolves
repos and rules itself):

- `--implement` — after consensus, claude implements the plan (acceptEdits)
  in the repos the agents identified. Refuses to touch a repo on
  main/master; changes stay uncommitted.
- `--iterations N` — raise the per-invocation review cap (default 5; verify
  rounds default 3).
- `--repos A,B` — review against exactly these repos for this run. When the
  plan lives in one of them and the project isn't known yet, the set is
  remembered for that project (see below); on a known project it acts as a
  one-off override and nothing is rewritten.
- `--strategy fresh|chained` — reviewer session policy (default `chained`).
  Chained: the reviewer resumes its own session every round — within a run
  and across re-runs — keeping prior rounds' context, which cuts per-round
  zcode token spend and lets it verify its earlier objections were resolved.
  `fresh` re-reviews independently each round (no anchoring on its own prior
  verdicts, at the cost of re-deriving everything every iteration).

### What happens in plan mode when you invoke it

1. The session writes a **context brief** (intent, constraints, decisions
   already made in-chat, out of scope) next to the plan — this is how the
   reviewer inherits your conversation's intent. The revising claude gets
   the fuller picture by forking the conversation that invoked the run —
   automatic and derived, not a flag: the engine command embeds a nonce that
   identifies exactly which chat triggered it, so re-triggering from the
   same chat reuses its context, and starting a new chat is the one way to
   reset it. (Stale context can therefore only come from staying in an old
   chat — a user decision, by design.) Whether a fork actually happens is
   the measured fork policy (0.9.0): the engine estimates the cost of
   re-sending the transcript versus a fresh reconstruction and picks, with
   the rationale logged and in the report — a cold oversized or
   tool-output-heavy transcript goes fresh automatically.
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

- The chained reviewer (default, `--strategy chained`) is the main lever on
  zcode spend: each round resumes the reviewer's own session instead of
  re-reading and re-deriving the plan from scratch, including on re-runs
  after decisions or rate limits.
- The drafter/fixer fork-vs-fresh choice is also automatic (`--fork-policy
  auto`, 0.9.0): the engine measures the invoking transcript (size,
  tool-output fraction) against the fresh-session reconstruction and forks
  only when forking is estimated to be the cheaper path within a 1.5x
  indifference margin. The decision and its numbers are logged and land in
  the run report (`fork_decision`).
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
| `skills/countersign/SKILL.md` | The countersign skill: intent doc -> consensus -> implement -> verify -> commit. The default entry point, model-triggered. |
| `commands/countersign.md` | The `/countersign` command: plan-consensus mode. Instructions the interactive session follows. |
| `scripts/countersign_loop.py` | The engine: agent invocation, verdict parsing, retries/rate limits, verify + commit gate. Full design doc in its docstring. |
| `tests/run_e2e.py` | End-to-end engine test with stub agents (zero model spend): `python3 tests/run_e2e.py` |

## Migrating from the terminal launcher (v0.1)

The `launch.sh` / `install.sh` terminal workflow is removed — the plugin is
the only interface now. If you installed the old global `countersign`
command, delete `~/.local/bin/countersign` and `countersign.cmd`. Existing
workspaces under `~/.countersign/ws/` are reused as-is by the plugin.

## Safety properties

- Product/company-direction decisions always stop for the human.
- Verify-mode commits refuse main/master BEFORE staging anything, and only
  happen after a clean review; plan mode never commits.
- Git push is never automated.
- The engine commits as the user's git identity with no agent attribution;
  reviewer minor notes ride the commit message for the record.
- API keys are never written to logs, stdout, or files.
- Unparseable reviewer output is treated as a blocking objection, never
  approval - and in verify mode, an unreadable review refuses to commit.
- Plan revisions are written atomically (temp file + rename): a run killed
  mid-write leaves the previous version or the new one, never a truncated mix.
- A run killed by SIGTERM/SIGHUP unwinds cleanly (`interrupted`, lock
  released); after SIGKILL, the next run automatically takes over the lock
  when its recorded holder PID is dead - no manual lock cleanup.
- A revision that comes back a fraction of the plan's size is treated as a
  truncated output turn, not a revision: the engine re-asks once, then stops
  with `revise-truncated` leaving the plan untouched at its last good state.
  (Each revise round re-emits the FULL document, so plans near/above ~100KB
  outgrow a single output turn — split them; the engine also warns up-front
  and scales the revise timeout with document size.)
