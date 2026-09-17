# Plan: countersign as an implementation skill (intent → consensus → implement → verify → commit)

## Goal

Move countersign's primary entry point from a user-typed command over a
pre-written plan document to a model-invoked skill triggered when the user
decides to implement what a chat discussion has already settled
("implement fix using countersign"). The skill derives a short intent doc
from the conversation, runs the existing consensus loop on it, then — after
consensus — the implementation is verified by a headless diff review and
committed on a feature branch. This removes the plan-document prerequisite
(the cause of the tool feeling redundant in real usage) and adds the missing
post-implementation review layer, while keeping the proactive gate at a cost
point where it is no longer redundant.

## Problem

- The plan doc prerequisite requires transcribing an already-settled design
  into a formal document before any review can run; in practice the design
  discussion with the human present has already done the proactive review,
  so the transcription-and-loop feels redundant and gets bypassed.
- The actual code edits — where implementation risk lives — receive no
  second opinion at all today.
- The `/countersign` command requires a plan path as its argument, baking
  the prerequisite into the interface itself.

## Non-goals

- No git push automation. Push stays a human action, always.
- The plan-consensus mode is NOT removed: it stays as the explicit opt-in
  for design-heavy changes (architecture, data models, API contracts,
  security boundaries, migrations), composable in front of the new pipeline.
- No change to the existing verdict schema, lock/PID takeover, rate-limit
  backoff, or fork-based revise machinery.

## Design

### 1. Skill interface

Add `skills/countersign/SKILL.md` to the plugin. The description encodes the
trigger: the user asks to implement a discussed change via countersign. The
skill body is THIN — it orchestrates; the engine decides:

1. Derive the intent doc from this conversation (below).
2. Run the engine (consensus mode) on the intent doc; mediate outcomes
   (open questions stop for the human in-chat, as today).
3. On consensus: implement the agreed approach in the linked repos, on a
   feature branch (create one if on main/master, offering the branch name).
4. Run the engine in verify mode; mediate outcomes.
5. On verify-clean the engine has committed; report and remind that push is
   manual.

`commands/countersign.md` remains as the plan-consensus entry point, its
description updated to say it is the design-review mode, not the default.
Naming: user-typed `/countersign` keeps meaning plan review; the skill is
model-triggered on implementation intent and routes pure plan-review
requests to the command. For fire-and-forget runs, consensus mode's
existing `--implement` (headless, post-consensus) composes with verify,
which reviews whatever the working tree holds — no separate headless
implement path is added.

### 2. Intent doc (replaces the plan prerequisite)

Written by the session, derived mechanically from the conversation — not a
transcribed formal plan. Under ~50 lines, sections:

- INTENT: the problem and why now
- CONSTRAINTS: technical constraints, things that must not change
- DECIDED: product decisions already settled in-chat (final; the loop must
  not re-open them — they seed `settled` decisions)
- APPROACH: how the fix/feature will be implemented (files, mechanisms)
- OUT OF SCOPE: what this effort does not touch

Location: `<plan-adjacent>/.countersign/<stem>-intent.md` — kept, not
deleted: it is the decision record the commit references. The context-brief
mechanism folds into this doc (one artifact, not two).

### 3. Consensus loop — unchanged

The existing engine loop runs on the intent doc as it runs on any plan:
GLM reviews, forked headless claude revises, open questions exit
blocked-on-human, chained reviewer by default (0.7.0). Intent docs are
small, so the truncation guards are dormant by construction.

### 4. Verify mode — new engine stage (`--verify`)

Second engine invocation after the session has implemented. Per linked repo:

- **Diff base**: `merge-base` of the current branch with the repo's default
  branch (main/master); diff that base through the working tree, so both
  commits and uncommitted changes are reviewed. Untracked new files are
  included (`git add -N` intent-to-add or equivalent, nothing staged for
  real).
- **Review**: the diff is attached; the consensus intent doc is the rubric
  (review prompt asks "does this diff satisfy the agreed intent and
  constraints", not "is this design sound"). The reviewer keeps workspace
  read access to verify claims against the code. Objections must cite
  `file:line` from the diff.
- **Feedback management**: iterate review→fix until zero BLOCKING
  objections. Severity gates the loop: blocking objections keep it alive;
  minors are fixed opportunistically in the same pass but never keep it
  running — they are recorded and carried into the commit message body.
  Each round re-reviews the FULL diff (regression catching), with the
  fixer's change summary appended so the chained reviewer knows what the
  last round claimed to fix. Per-round diffs snapshot to the history dir
  (`diff-v1.patch` …).
- **Fixer**: headless claude, forked from the invoking conversation (same
  machinery as revise), applies objections to the working tree.
- **Cap**: default 3 rounds (lower than consensus mode's 5). Exits:
  verify-clean | verify-no-consensus (remaining blockers surfaced; human may
  fix in-chat and re-run, or commit anyway WITH objections written into the
  commit message — an explicit, auditable override) | blocked-on-human |
  rate-limited | interrupted (all existing semantics unchanged).
- **Size guard**: diff above the attach cap is batched per file (verdicts
  merged by the engine); feasibility warning reuses the plan-size
  scaffolding.
- **Stage-scoped reviewer seeding**: verify seeds its reviewer only from its
  own stage's records (`verify-review-iter-*.json`), never from the
  consensus stage's `review-iter-*.json` — the 0.7.0 cross-invocation
  seeding is scoped per stage so the two gates stay independent (see
  Resolved decisions).

### 5. Commit gate — deliberate invariant change

After verify-clean, the ENGINE commits (machine-enforced, not
prose-instructed): per touched repo, refusing main/master exactly as the
implement pass does, as the user's configured git identity with no
Co-Authored-By or agent-attribution trailers, message derived from the
intent doc (title + intent summary + minor-objective notes in the body +
`countersign: verified` trailer). Nothing is ever pushed. The "nothing is
ever committed" invariant is superseded by "nothing is ever committed off a
feature branch, and never before a clean verify"; README and docstring must
say so.

### 6. Glue migration into the engine

Repo-set resolution, intent-doc defaulting, and diff-base detection move
into `countersign_loop.py` so that direct engine invocation — which agents
already do — is correct usage. SKILL.md shrinks to trigger recognition +
intent-doc shape + outcome mediation; the long step-by-step protocol in the
current command file (brief writing, repo resolution, hash capture) is
absorbed rather than duplicated.

## Changes (by file)

- `skills/countersign/SKILL.md` — new; the implement-flow skill.
- `commands/countersign.md` — repositioned as plan-consensus mode; steps 4-5
  (brief, engine glue) slim down to "the engine derives this".
- `scripts/countersign_loop.py` — `--verify` mode: diff-base detection,
  diff generation/attach with size batching, verify review prompt (rubric =
  intent doc, file:line citations, severity-gated termination), fixer loop
  (reuse fork machinery), commit gate, `diff-v*.patch` snapshots, new exit
  codes (`verify-no-consensus`), glue absorption (repo resolution +
  intent-doc defaulting).
- `tests/run_e2e.py`, `tests/stub_zcode.py` — scenarios below.
- `README.md` + `plugin.json` — new flow diagram, safety-properties rewrite
  (commit invariant), version 0.8.0.

## Testing

E2E, zero model spend (stub agents, real git repos as today):

- V1 verify-clean: implement edits in a fake repo on a feature branch →
  stub approves diff → engine commits; assert commit exists on the feature
  branch, message carries intent title + trailer, main untouched.
- V2 verify loop: round 1 blocking objection → forked fixer edits the file →
  round 2 clean → commit; assert per-round `diff-v*.patch` snapshots and
  that round 2's review saw round 1's fix summary.
- V3 severity gating: approve-with-minors exits verify-clean; minor text is
  in the commit message body; loop did NOT spend another round on minors.
- V4 cap exit: persistent blocking objections → verify-no-consensus; working
  tree holds the last fix round; no commit.
- V5 commit guard: repo on main/master → refuse, nothing edited, no commit.
- V6 uncommitted + untracked coverage: a modified tracked file AND a new
  untracked file both appear in the reviewed diff.
- V7 open question mid-verify → blocked-on-human, `--decisions` resume works.

## Rollout

Ship as 0.8.0. README leads with the skill flow; plan-consensus documented
as the opt-in design gate. Migration note: `/countersign` keeps working
unchanged for plan mode; the skill needs no install steps beyond updating
the plugin.

## Resolved decisions

1. **Commit attribution: none.** The engine commits with the user's
   configured git identity and no Co-Authored-By or agent trailers. The
   human is the author of record — countersign never pushes, and the
   `countersign: verified` trailer is the provenance. This also keeps
   DCO/CLA tooling and history audits unconfused.
2. **Verify starts a FRESH reviewer session; chaining applies within the
   verify stage only.** The two gates ask different questions ("is this
   design sound?" vs "does this diff satisfy the contract?"), and a reviewer
   that approved the plan would be reviewing its own approved design's
   implementation. The verify reviewer reads the consensus intent doc cold
   as its rubric; within verify rounds the 0.7.0 chained default applies,
   which is where the spend actually concentrates. Engine consequence:
   per-stage seeding scoping (above).
3. **No new headless implement path.** The interactive session implements
   between the two engine invocations (drafter of record, user watches);
   existing `--implement` already serves fire-and-forget and composes with
   verify. Adding a second headless implement path would duplicate the
   branch-guard surface for no new capability.
