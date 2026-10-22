---
name: delegate
description: Hand a well-specified implementation task to GLM through the headless ZCode CLI so the output tokens are spent on the z.ai coding plan. Use when the user asks to delegate/outsource a task to GLM, or when a task fits the delegation criteria below. The session keeps the judgment work - it writes the brief and MUST review the result; GLM's own report is never verification.
---

# delegate - outsource implementation to GLM, keep the judgment

You (the Claude session) write the brief and review the result. GLM
implements it in an isolated git worktree; nothing touches the caller's
tree until you run `apply` after review. The runner is
`${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py` (resolve relative to this
file: `../../scripts/delegate.py`); run it with `python3`.

## When to delegate

- implementing a section of a reviewed plan (e.g. a countersign plan that
  reached consensus - see "Pairing with countersign" below);
- writing tests from a spec;
- mechanical refactors and renames;
- i18n keys, scenario files and boilerplate, including new files.

Suggest delegating when a task fits and the user is watching quota, but
never delegate silently - say what you would hand off and why.

## When NOT to delegate

- changes under about 50 lines - you will spend more on the brief and the
  review than the work is worth;
- interactive debugging;
- anything that touches databases, credentials, git history or production;
- design decisions - those are yours (or the human's), not GLM's.

## The brief

Write the brief to a file first (e.g. `.countersign/delegate-<slug>.md`).
Template - every heading required:

```markdown
# <task title>

## Goal
<what "done" means, in one or two sentences>

## Read first
- <exact paths, one per line>

## Do
1. <numbered, concrete steps>

## Do not
- <boundaries: files/dirs not to touch, behaviors to preserve>

## Checks to run
<the exact commands that prove the work>

## Final reply
<the exact final message wanted; it must include: the list of files
changed; a summary of each change; which checks ran and their results;
any existing test you changed and why; anything you decided that the
brief did not say>
```

The runner prepends fixed hard rules (no commit/push/stash/reset/rebase/
merge/cherry-pick, no branch or worktree changes, no edits outside the
worktree, no .env/keys) and enforces them best-effort with a git shim on
GLM's PATH - an absolute-path git call still bypasses the shim, which is
why your review is mandatory.

## Running it

Run it as a BACKGROUND task (the run takes minutes; stderr carries a
heartbeat, stdout ends with exactly ONE JSON line):

```bash
python3 <runner> run --brief <brief.md> [--repo <path>]... [--check "<cmd>"]...
```

Defaults: reasoning effort `high`, `--on-limit return`, permission mode
`yolo`, repo = the git repo containing the current directory. Repeat
`--repo` per repo for multi-repo tasks; repeat `--check` for commands run
in the worktree(s) after GLM finishes (prefix `<repo>:<cmd>` to scope one;
a failed check never changes the outcome - it is reported for your
review). `--setup "<cmd>"` runs before GLM (e.g. an offline install) and a
failed setup stops the run.

Read the last stdout line as JSON and branch on `outcome`:

- `done` - GLM finished. Review now (below). `repos[].files` lists every
  changed file (new files have `status: "A"`), `checks[]` carries each
  check's exit code and output tail, `model_served` is the model + effort
  that actually served the run, `usage` the CLI-reported token totals
  (fields null when the CLI carried no usage), `quota` the z.ai window
  shares before/after, `warnings` always includes the shim's
  best-effort caveat plus any gitignored files GLM created that the diff
  excludes.
- `rate-limited` - the z.ai quota refused the run (pre-check) or the
  limit hit mid-run. Tell the human the reset time (`quota.resets`) and
  offer `resume <run-id>` later. NEVER retry in a loop.
- `timeout` / `failed` / `refused` / `interrupted` - report `error`; fix
  the cause (brief, environment, quota) and re-run.
- `locked` - another delegate run holds the shared zcode config; wait for
  it.

`--on-limit wait` exists but only mention it when the human has explicitly
accepted an unattended wait: it sleeps until the real window reset (plus 3
minutes) and continues the session, at most `--max-waits` times (default 1).

Resume after a stop continues the SAME GLM session:
`python3 <runner> resume <run-id>`. `status <run-id>` reports persisted
state at any time.

## The review - MANDATORY, before apply

Never relay GLM's "all tests pass" as verified. Do all of this yourself:

1. Read the diffstat including the new files (`repos[].files`; or
   `git -C <worktree> diff --stat <base>`).
2. Read the risky files in full - the ones touching auth, payments, data
   loss, concurrency, or anything the plan called out.
3. Re-run the checks yourself in the worktree. GLM's check results are
   its claim, not proof.
4. Spot-check how the new code connects to existing callers and flows -
   the measured defects (a pricing page that could never reach the trial
   offer, wrong timing semantics, a check that could never fire, the
   wrong API key and auth header) were all integration gaps, not bad files.
5. Look for decisions GLM made silently (`reply` says what it admits to;
   the diff says what it did).
6. Fix what you find - edit the worktree directly, or `resume <run-id>`
   with instructions.
7. Only then integrate and clean up:

```bash
python3 <runner> apply <run-id>    # lands the diff UNCOMMITTED in your tree
python3 <runner> clean <run-id>    # removes the worktree + glm/<run-id> branch
```

`apply` refuses if your tree has uncommitted changes in the same paths
(`--force` overrides after you resolve them); `clean` refuses while the
worktree holds unapplied work and names the paths (`--force` discards).
Nothing is ever committed or pushed by the runner - the commit is yours.

## Pairing with countersign

A plan that reached consensus can be delegated section by section: one
delegate run per plan section, each with its own brief whose "Read first"
names the plan and the sections already built. Alternatively, run the
whole implement+verify pipeline with GLM as implementer:
`python3 <engine> <intent-doc> --implement --implementer glm` then
`--verify --implementer glm` (the engine drives delegate for you; Claude
reviews and the commit gate is unchanged). See the countersign skill.
