---
name: countersign
description: Consensus-gated implementation. Use when the user asks to implement a change or fix discussed in this chat via countersign (e.g. "implement fix using countersign"). Derives a short intent doc from this conversation, runs the GLM consensus loop on it, implements on a feature branch, then headlessly verifies the diff against the intent and commits. For reviewing a standalone plan document without implementing, use the /countersign command (plan mode) instead.
---

# countersign - consensus-gated implement + verify + commit

You (the interactive session) are the DRAFTER OF RECORD: you hold this
conversation's context. The engine does the looping; you mediate between
it and the human. Nothing is ever pushed; commits happen only through the
engine's verify gate.

The engine script lives at `${CLAUDE_PLUGIN_ROOT}/scripts/countersign_loop.py`
(if unset, resolve relative to this file: `../../scripts/countersign_loop.py`).
Run it with `python3` (or `python`).

## Stage 1 - intent doc

Write `<cwd>/.countersign/intent-<slug>.md` (create the dir; slug = a few
words from the task). Under ~50 lines, derived from THIS conversation - not
a formal plan transcription. Sections:

- INTENT: the problem and why now
- CONSTRAINTS: technical constraints, existing decisions, things that must
  not change
- DECIDED: product decisions already settled in-chat (final)
- APPROACH: how it will be implemented (files, mechanisms)
- OUT OF SCOPE: what this effort does not touch

If the discussion left the approach genuinely undecided, ask the human
FIRST; never invent one silently.

## Stage 2 - consensus on the intent doc

Run the engine as a BACKGROUND task (the loop runs minutes; stderr carries
a heartbeat, stdout ends with exactly ONE JSON line):

```bash
python3 <engine> "<intent doc path>" --fork-invocation-nonce "cs-$(date +%s)-$RANDOM"
```

No repo flags needed: the engine resolves the repo set itself (saved
project sets by membership, else the intent doc's own repository). If the
intent doc is not inside a git repository and the work targets a specific
repo, pass `--link-repo <path>` explicitly.

Parse the last stdout line as JSON; surface `warnings` verbatim. Branch:

- consensus -> Stage 3
- blocked-on-human -> ask the human each question from
  `open_questions_file` IN THIS CHAT (options + reviewer recommendation),
  fill `answer` fields, save as `<history_dir>/decisions.json`, re-run with
  `--decisions "<file>"`. Repeat.
- rate-limited -> tell the human; state is persisted; re-run later
  continues. Do not retry now.
- locked / plan-mismatch / error / interrupted -> report per the report's
  `error`/`warnings`; fixes are re-running unchanged, re-reading the file,
  or a fresh chat (stale fork context).

## Stage 3 - implement

On consensus: create/switch to a feature branch in each target repo
(never implement on main/master; propose a branch name if creating).
Implement the APPROACH from the intent doc. Keep changes uncommitted.

## Stage 4 - verify + commit

Re-run the engine in verify mode (background, same JSON contract):

```bash
python3 <engine> "<intent doc path>" --verify --fork-invocation-nonce "cs-<new-nonce>"
```

The engine diffs each linked repo (merge-base with its default branch
through the working tree), reviews against the intent doc, fixes blocking
objections with a forked headless session between rounds, and on a clean
review COMMITS each touched repo (refusing main/master; message carries
minor-reviewer notes and a `countersign: verified` trailer). Branch:

- verify-clean -> report the commit sha(s) and what the reviewer validated
  (strengths) + any fyi_notes; remind the human that pushing is theirs.
- verify-no-consensus -> show the remaining blocking objections; options:
  fix together in-chat and re-run verify, or the human explicitly accepts
  committing with objections on record (re-run and accept when the human
  says so).
- blocked-on-human / blocked-on-branch / rate-limited / others -> same
  handling as Stage 2; branch-blocked means create the feature branch,
  then re-run verify unchanged.
