---
description: Dual-agent consensus on a planning doc - GLM reviews, headless Claude revises, loop until consensus
argument-hint: <plan-file.md> [--implement] [--iterations N] [--repos A,B]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /countersign - dual-agent consensus loop

You (the interactive session) are the DRAFTER OF RECORD: you hold this
conversation's context. The engine below runs the loop headlessly - ZCode
(GLM) reviews the plan, a headless claude session revises it, repeat until
the reviewer approves. Your job before the loop: prepare inputs. Your job
after: mediate results and human decisions back into this chat.

Arguments: `$ARGUMENTS`
Parse them: one plan file path (required), plus optional flags
`--implement`, `--iterations N`, `--repos <p1>,<p2>,...`. If no plan path is
given, ask the user which planning document to review before doing anything
else.

## Step 1 - locate and verify the plan

- Resolve the plan path relative to the current working directory. If it
  does not exist, use Glob to look for likely candidates (`**/docs/plans/*.md`)
  and confirm with the user; never guess silently.
- Read the plan file. If it is clearly a stub or empty, ask the user whether
  you should finish drafting it in-chat first - the engine never drafts.
- Immediately after reading it, capture its content fingerprint
  (`sha256sum "<plan path>"`) and remember it — you will pass it to the
  engine as `--expect-sha256` so it refuses to run if the file on disk turns
  out to be a different version than the one you read (wrong-branch
  worktree, base-commit mixup, mid-session edit).
- Before launching, tell the user which branch and commit of the plan's
  repo you are about to review (the engine logs it too). If a worktree is
  active, verify it is based on the user's actual branch — a worktree
  silently based on `origin/main` while the user works on `dev` reviews a
  stale plan.

## Step 2 - device setup (once per device)

- Check for `~/.countersign/preflight-ok`. If missing, run the engine once
  with `--preflight` first (see command shape below, replacing the plan file
  with `--preflight x`). On success, write the marker file. On failure, show
  the user the failures and stop - do not run the loop.
- Nothing else is device-global. Repo sets are PER PROJECT (Step 3): never
  write or consult a device-wide repo list - applying one project's repos to
  another project's plan links the wrong code.

## Step 3 - resolve the repo set for THIS plan

- Find the plan's repo root:
  `git -C "<plan dir>" rev-parse --show-toplevel` (empty output means the
  plan is not inside a git repository).
- If the user passed `--repos <p1>,<p2>,...`: expand `~`, resolve each to an
  absolute path, and use exactly those for this run. Additionally, if the
  plan's repo root IS among them AND no existing project entry (see below)
  already contains the plan's repo root, remember the set: read
  `~/.countersign/config.json` (create `{}` if missing), ensure a `projects`
  object, store `{"repos": ["<abs path>", ...]}` under a key named after the
  plan repo root's directory (e.g. `ladderly_backend`), and tell the user in
  one line that the set was remembered for this project. Never persist when
  an entry already covers the repo - a later `--repos` on a known project is
  a deliberate one-off override, not a redefinition.
- Else read `~/.countersign/config.json`. If it has a `projects` object,
  resolve each entry's `repos` and use the FIRST entry whose list contains
  the plan's repo root: `REPOS` = that entry's full list. Membership, not
  direction: a plan written in the backend repo of a backend+frontend
  project resolves to BOTH repos, and so does one written in the frontend.
- Legacy migration: if the config has a flat `repos` array and no `projects`
  object, rewrite it in place as
  `{"projects": {"migrated": {"repos": <that array>}}}` and tell the user in
  one line. The old flat array applied one repo set to every plan on the
  device, which linked the wrong repos when switching projects.
- Otherwise (no config, or no entry contains the plan's repo root):
  `REPOS` = the plan's repo root ALONE. Do not ask the user anything and do
  not mention other projects' repos - a plan in an unconfigured repo is
  reviewed against exactly that repo. If the plan is not inside a git
  repository at all, pass no `--link-repo` flags (the engine then uses the
  session's working directory as the workspace).

## Step 4 - write the context brief

This is how the headless agents inherit THIS conversation's context cheaply.
Write `<plan-dir>/.countersign/<plan-stem>-context-brief.md` (create the
`.countersign` dir; the per-plan name keeps concurrent runs on sibling plans
from overwriting each other). Keep it under ~40 lines. It must capture, from
this conversation and the plan itself:

- INTENT: what problem this plan addresses and why now
- CONSTRAINTS: technical constraints, existing decisions, things that must
  not change (e.g. "reuse the existing production VAPID keys - new keys would
  break existing subscribers")
- DECIDED: product decisions already made in-chat (these are final; the
  agents must not re-open them)
- OUT OF SCOPE: what this effort explicitly does not touch

If this conversation has no relevant context (the plan was handed to you
cold), derive the brief from the plan's own Goal/Non-goals sections and say
so in the brief.

## Step 5 - run the engine

Build and run with Bash (adjust flags from the parsed arguments):

```bash
NONCE="cs-$(date +%s)-$RANDOM$RANDOM"
python "${CLAUDE_PLUGIN_ROOT}/scripts/countersign_loop.py" "<plan path>" \
  --expect-sha256 "<hash you captured when reading the plan>" \
  --fork-invocation-nonce "$NONCE" \
  $(printf -- '--link-repo %q ' "${REPOS[@]}") \
  --context-brief "<plan-dir>/.countersign/<plan-stem>-context-brief.md" \
  [--decisions "<decisions file>" (only when resuming after answers)] \
  [--implement] \
  [--max-iterations N (only when --iterations given)]
```

- `${REPOS[@]}` are the repo paths resolved in Step 3.
- If the plan's repo root (walk up from the plan file to the containing git
  repository) has an `agent-review-rules.md`, also pass
  `--review-rules "<that file>"`.
- Do not filter or pipe the engine's output; it ends with ONE JSON line on
  stdout that you must parse. Expect the run to take minutes; the engine
  streams progress to stderr with a heartbeat.
- The engine forks THIS conversation for the revise calls - automatically,
  no flags, no decision to make. The nonce in the command above identifies
  this exact chat (the command line carrying it is recorded in this chat's
  transcript before the engine runs), so the fork target is derived, never
  guessed. Re-triggering /countersign from this same chat re-forks the same
  conversation; starting a NEW chat is the one and only way the context
  resets. If a run ever seems anchored to stale context, the fix is a new
  chat - by design, that is the only usage error left to make. The context
  brief from Step 4 is still required: the reviewer (zcode) never sees the
  forked conversation, only the brief.

## Step 6 - mediate the outcome

Parse the last stdout line as JSON, surface any `warnings` in the report
verbatim (they flag e.g. a plan/checkout mismatch), and branch on `outcome`:

- **consensus** - Consensus means the reviewer approved with ZERO objections
  of any severity; improvement suggestions raised in earlier rounds were
  incorporated by the drafter. Read the final plan. Tell the user three
  things: (1) what changed between the first reviewed draft and the final
  plan (the `<history_dir>/plan-v*.md` snapshots are the record, including
  which reviewer suggestions were applied); (2) what the reviewer
  explicitly VALIDATED as strong (`strengths` in the report - the parts of
  the design that held up, so the user knows what not to second-guess);
  (3) any `fyi_notes` from the report. If `implement_attempted` is true,
  list the edited repos and remind the user changes are uncommitted
  (`git diff` to review). Nothing is ever committed or pushed by this
  command.
- **blocked-on-human** - Read `open_questions_file`. Ask the user each
  question IN THIS CHAT, showing options and the reviewer's recommendation.
  After the user answers, fill the `answer` fields into that JSON, save it
  as `<history_dir>/decisions.json`, and re-run the engine exactly as before
  plus `--decisions "<history_dir>/decisions.json"`. Decisions are
  CUMULATIVE across rounds: if a decisions.json from an earlier round
  exists, keep its answered entries (this file may carry only the newest
  delta - the engine also persists settled answers in
  `<history_dir>/settled-decisions.json` and merges, the file's answer
  winning on the same question). To change a settled answer, pass the new
  answer via decisions.json; to drop one entirely, also remove it from
  settled-decisions.json. Loop back to this step.
- **blocked-on-branch** - Report which repos sit on main/master (nothing was
  edited). Offer to create feature branches; if the user agrees, create
  them, then re-run the engine unchanged. Loop back to this step.
- **rate-limited** - Tell the user the 5h/weekly window appears spent; the
  run's state is persisted (plan snapshots + reviews in `history_dir`), so
  re-running later continues rather than re-spending earlier iterations.
  Stop; do not retry automatically.
- **revise-truncated** - The drafting agent's revision came back a small
  fraction of the plan's size even after one re-ask: a truncated output
  turn, not a real revision. The plan file was NOT modified - the last good
  version stands. Tell the user, surface any `warnings` from the report,
  and offer the real options: split the plan into smaller documents (each
  revise round re-emits the FULL document, and plans near/above ~100KB
  exceed one output turn), or make this round's revision together in-chat
  and re-invoke on the updated file.
- **locked** - Another countersign run is already working on this same plan
  (check for a background task in this or another session). Tell the user;
  do not delete the lock unless they confirm the other run is dead.
- **plan-mismatch** - The plan on disk is NOT the version you read (wrong
  branch/worktree or it changed under you). Do NOT proceed. Re-locate the
  correct file, read it fresh, capture the new hash, tell the user what
  happened, and re-invoke with the new `--expect-sha256`.
- **no-consensus** - Show the remaining blocking AND minor objections from
  the report verbatim (minors are improvement suggestions the reviewer still
  wants made). Offer the user the real options: raise `--iterations`, revise
  the plan together in-chat first, or accept the disagreement and stop.
- **error** - Show `error` from the report and the tail of the engine's
  stderr output; suggest the likely fix.

When re-running the engine (any branch above), always reuse the same plan
path and flags, adding only what that branch requires; generate a fresh
NONCE each time (any new value identifies this same chat identically).
