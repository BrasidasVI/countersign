---
description: Plan mode - dual-agent consensus on a planning doc (GLM reviews, headless Claude revises, loop until consensus). For implementing a discussed change, use the countersign skill instead.
argument-hint: <plan-file.md> [--implement] [--iterations N]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /countersign - plan-consensus mode (design review)

This is the DESIGN-REVIEW entry point: run it on a plan document the human
wants stress-tested BEFORE implementation - architecture changes, data
model or API contract changes, security boundaries, migrations. It is the
explicit opt-in gate, not the default path: when the human asks to
IMPLEMENT something discussed in chat, use the countersign skill instead
(intent doc -> consensus -> implement -> verify -> commit).

You (the interactive session) are the DRAFTER OF RECORD: you hold this
conversation's context. The engine below runs the loop headlessly - ZCode
(GLM) reviews the plan, a headless claude session revises it, repeat until
the reviewer approves. Your job before the loop: prepare inputs. Your job
after: mediate results and human decisions back into this chat.

Arguments: `$ARGUMENTS`
Parse them: one plan file path (required), plus optional flags
`--implement`, `--iterations N`. If no plan path is given, ask the user
which planning document to review before doing anything else.

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
  repo you are about to review (the engine logs it too).

## Step 2 - device setup (once per device)

- Check for `~/.countersign/preflight-ok`. If missing, run the engine once
  with `--preflight` first (replacing the plan file with `--preflight x`).
  On success, write the marker file. On failure, show the user the failures
  and stop - do not run the loop.

## Step 3 - write the context brief

This is how the headless agents inherit THIS conversation's context cheaply.
Write `<plan-dir>/.countersign/<plan-stem>-context-brief.md` (create the
`.countersign` dir). Keep it under ~40 lines. It must capture, from this
conversation and the plan itself:

- INTENT: what problem this plan addresses and why now
- CONSTRAINTS: technical constraints, existing decisions, things that must
  not change
- DECIDED: product decisions already made in-chat (these are final; the
  agents must not re-open them)
- OUT OF SCOPE: what this effort explicitly does not touch

If this conversation has no relevant context (the plan was handed to you
cold), derive the brief from the plan's own Goal/Non-goals sections and say
so in the brief.

## Step 4 - run the engine

```bash
NONCE="cs-$(date +%s)-$RANDOM$RANDOM"
PYTHON="$(command -v python3 || command -v python)"
"$PYTHON" "${CLAUDE_PLUGIN_ROOT}/scripts/countersign_loop.py" "<plan path>" \
  --expect-sha256 "<hash captured in step 1>" \
  --fork-invocation-nonce "$NONCE" \
  --context-brief "<plan-dir>/.countersign/<plan-stem>-context-brief.md" \
  [--decisions "<decisions file>" (only when resuming after answers)] \
  [--implement] \
  [--max-iterations N (only when --iterations given)]
```

The engine resolves everything else itself: the repo set (saved project
sets matched by membership, else the plan's own repository - pass
`--link-repo <path>` only when the human explicitly names repos to review
against) and per-repo `agent-review-rules.md` (auto-detected at the plan's
repo root).

- LAUNCH THIS AS A BACKGROUND TASK, never a plain foreground Bash call: a
  full loop routinely runs longer than a foreground tool timeout. Use the
  Bash tool's run_in_background mode. stderr carries a progress heartbeat;
  stdout ends with exactly ONE JSON line (in background mode: the last line
  of the captured output file). Do not filter or pipe the output.
- The engine forks THIS conversation for the revise calls (the nonce
  identifies this exact chat). The context brief is still required: the
  reviewer (zcode) never sees the forked conversation, only the brief.

## Step 5 - mediate the outcome

Parse the last stdout line as JSON, surface any `warnings` verbatim, and
branch on `outcome`:

- **consensus** - Read the final plan. Tell the user: (1) what changed
  between the first reviewed draft and the final plan
  (`<history_dir>/plan-v*.md` snapshots are the record); (2) what the
  reviewer explicitly VALIDATED (`strengths` - what not to second-guess);
  (3) any `fyi_notes`. If `implement_attempted` is true, list the edited
  repos and remind the user changes are uncommitted. Nothing is ever
  committed or pushed by plan mode.
- **blocked-on-human** - Read `open_questions_file`. Ask the user each
  question IN THIS CHAT, showing options and the reviewer's recommendation.
  After the user answers, fill the `answer` fields, save as
  `<history_dir>/decisions.json`, and re-run the engine exactly as before
  plus `--decisions "<file>"`. Decisions are CUMULATIVE: keep earlier
  answered entries (`settled-decisions.json` also merges; the file's answer
  wins). Loop back to this step.
- **blocked-on-branch** - Report which repos sit on main/master (nothing
  was edited). Offer to create feature branches; if the user agrees, create
  them, then re-run the engine unchanged.
- **rate-limited** - The 5h/weekly window appears spent; state is
  persisted, so re-running later continues rather than re-spending. Stop.
- **revise-truncated** - The revision came back a small fraction of the
  plan's size even after one re-ask. The plan file was NOT modified. Offer
  the real options: split the plan, or revise together in-chat and
  re-invoke.
- **locked** - Another run genuinely holds this plan's lock (dead runs'
  locks are taken over automatically). Check for a background task; do not
  delete the lock unless the human confirms the other run is dead.
- **plan-mismatch** - The plan on disk is NOT the version you read. Do NOT
  proceed. Re-locate, re-read, re-hash, tell the user what happened,
  re-invoke with the new `--expect-sha256`.
- **no-consensus** - Show the remaining blocking AND minor objections
  verbatim. Options: raise `--iterations`, revise in-chat first, or accept
  the disagreement and stop.
- **error** - Show `error` and the stderr tail; suggest the likely fix.
- **interrupted** - The engine was killed mid-run; lock released, plan
  intact at its last fully-written version. Re-run unchanged to continue.

When re-running, always reuse the same plan path and flags, adding only
what that branch requires; generate a fresh NONCE each time.
