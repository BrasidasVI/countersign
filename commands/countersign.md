---
description: Dual-agent consensus on a planning doc - GLM reviews, headless Claude revises, loop until consensus
argument-hint: <plan-file.md> [--implement] [--fork] [--iterations N]
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
`--implement`, `--fork`, `--iterations N`. If no plan path is given, ask the
user which planning document to review before doing anything else.

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

- Check for `~/.countersign/config.json`. If missing (or lacking a `repos`
  array), ask the user for the repository paths involved in this plan
  (typically a backend and a frontend repo, absolute paths), then write:
  `{ "repos": ["<abs path 1>", "<abs path 2>"] }`.
- Check for `~/.countersign/preflight-ok`. If missing, run the engine once
  with `--preflight` first (see command shape below, replacing the plan file
  with `--preflight x`). On success, write the marker file. On failure, show
  the user the failures and stop - do not run the loop.

## Step 3 - write the context brief

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

## Step 4 - run the engine

Build and run with Bash (adjust flags from the parsed arguments):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/countersign_loop.py" "<plan path>" \
  --expect-sha256 "<hash you captured when reading the plan>" \
  $(printf -- '--link-repo %q ' "${REPOS[@]}") \
  --context-brief "<plan-dir>/.countersign/<plan-stem>-context-brief.md" \
  [--decisions "<decisions file>" (only when resuming after answers)] \
  [--implement] \
  [--strategy chained --fork-current-session (only when --fork given)] \
  [--max-iterations N (only when --iterations given)]
```

- `${REPOS[@]}` are the entries from `~/.countersign/config.json`.
- If the plan's repo root (walk up from the plan file to the containing git
  repository) has an `agent-review-rules.md`, also pass
  `--review-rules "<that file>"`.
- Do not filter or pipe the engine's output; it ends with ONE JSON line on
  stdout that you must parse. Expect the run to take minutes; the engine
  streams progress to stderr with a heartbeat.
- `--fork` costs more on every revise call (it re-sends this whole
  conversation's context) - only use it when the user asked for it.

## Step 5 - mediate the outcome

Parse the last stdout line as JSON and branch on `outcome`:

- **consensus** - Read the final plan. Tell the user three things: (1) what
  changed between the first reviewed draft and the final plan (the
  `<history_dir>/plan-v*.md` snapshots are the record); (2) what the reviewer
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
  plus `--decisions "<history_dir>/decisions.json"`. Loop back to this step.
- **blocked-on-branch** - Report which repos sit on main/master (nothing was
  edited). Offer to create feature branches; if the user agrees, create
  them, then re-run the engine unchanged. Loop back to this step.
- **rate-limited** - Tell the user the 5h/weekly window appears spent; the
  run's state is persisted (plan snapshots + reviews in `history_dir`), so
  re-running later continues rather than re-spending earlier iterations.
  Stop; do not retry automatically.
- **locked** - Another countersign run is already working on this same plan
  (check for a background task in this or another session). Tell the user;
  do not delete the lock unless they confirm the other run is dead.
- **plan-mismatch** - The plan on disk is NOT the version you read (wrong
  branch/worktree or it changed under you). Do NOT proceed. Re-locate the
  correct file, read it fresh, capture the new hash, tell the user what
  happened, and re-invoke with the new `--expect-sha256`.
- **no-consensus** - Show the remaining blocking objections from the report
  verbatim. Offer the user the real options: raise `--iterations`, revise
  the plan together in-chat first, or accept the disagreement and stop.
- **error** - Show `error` from the report and the tail of the engine's
  stderr output; suggest the likely fix.

When re-running the engine (any branch above), always reuse the same plan
path and flags, adding only what that branch requires.
