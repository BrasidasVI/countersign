# Plan: `delegate`: hand a well-specified task to GLM through the headless ZCode CLI

Implementor: GLM-5.3 via ZCode (reviewed by the owner's Claude session)

## Goal

Let a Claude Code session outsource implementation work to GLM-5.3. The output tokens for that work are then spent on the z.ai coding plan instead of the Anthropic plan. The Claude session keeps the judgment work: it writes the brief and reviews the result.

The plugin gains a second skill, `delegate`. A script backs it. The script runs one task through the headless ZCode CLI and returns one compact JSON line.

## Problem

Measured on 2026-10-22 during the ladderly premium-trial build. Three GLM phases were driven by an ad-hoc shell script.

- **The saving is real.** One frontend phase was a single GLM turn:
  - about 1.27M tokens in, mostly cached;
  - about 6K tokens out;
  - 3 minutes;
  - about 35 files.

  Reviewing the diff cost the Claude session mainly input tokens.
- **Review is mandatory.** Every phase compiled and passed its own tests, and every phase still had a real defect:
  - an integration gap: a pricing page still checked out at full price, so the trial offer could never be reached;
  - wrong timing semantics;
  - a check that could never fire;
  - the wrong API key and auth header.

  The misses were in how pieces fit together.
- **z.ai quota is the bottleneck, not tokens.**
  - On the Lite plan, GLM used up a 5-hour window in 11–30 minutes at high or max effort.
  - When it hit a limit, the ad-hoc script slept a fixed 5h05m. That overshot a known reset by two hours.
  - The script also changed the effort setting, which then had to be restored by hand.
- **Sharing a working tree is fragile.** GLM edited the same tree the owner was editing in parallel.

Nothing in the plugin does this today. The engine only uses ZCode as a reviewer.

## Non-goals

- **Other providers or models.** Only the ZCode coding-plan catalog is supported. Today that is GLM-5.3 and GLM-5.3-Flash.
- **Automatic integration.** GLM's work is never merged, committed or pushed into the caller's branch automatically. Integration is an explicit step the caller takes after review.
- **Parallel delegate runs.** Not in v1. Runs are serialized because the model and effort settings live in shared config files and the quota is shared.
- **In-place runs.** Not in v1. Every run uses a separate worktree. There is no `--in-place` option.
- **Engine changes beyond section 5.** The consensus loop's behaviour and verdict schema stay as they are. The CLI only gains the options in section 5, and with none of them given the engine behaves exactly as today.
- **Skipping review.** The skill never treats GLM's own report as verification.

## Settled decisions

- The default reasoning effort for delegated work is `high`. `--effort` and `delegate.effort` in the config override it.
- `--on-limit` defaults to `return`.
- Countersign's implementation step can be done by Claude or by GLM. When GLM implements, Claude reviews by default (section 5).

## Design

### 1. Shared ZCode helpers

The helpers in `scripts/countersign_loop.py` move unchanged into `scripts/zcode_common.py`. The engine imports them from there:

- `resolve_zcode_cmd`, `_node_prefix`, `_zcode_explicit_cmd`
- `_zcode_provider_env`, `_zcode_failure_hint`, `heal_zcode_cli_config`
- `zcode_api_key`, `zcode_model_desc`, `zcode_configured_model`
- `RATE_LIMIT_MARKERS`, `_looks_rate_limited`
- `_run` (with its heartbeat), `_loads_cli_json`, `extract_json_object`
- the code that pulls per-call usage out of the ZCode CLI's JSON output. Today the engine uses it to collect "zcode usage entries" for each reviewer call. If it is inline, it is first extracted, unchanged, into a named function.

Rules for the move:

- There is no behaviour change. `tests/run_e2e.py` passes unchanged.
- The engine keeps re-exporting the moved names, so today's tests and any external caller keep working.

### 2. `scripts/delegate.py`: the runner

The runner has five subcommands:

- `run`: start a task.
- `resume <run-id>`: continue the same GLM session after a stop.
- `status <run-id>`: report a run's state.
- `apply <run-id>`: put the run's diff into the caller's tree.
- `clean <run-id>`: remove the run's worktree and branch.

Each subcommand prints exactly one JSON line on stdout. Progress and the heartbeat go to stderr. This is the same contract as the engine.

`delegate.py run --brief <file> [options]` takes these options:

- `--repo <path>`: may be repeated. The default is the git repo that contains the current directory.
- `--model GLM-5.3|GLM-5.3-Flash`
- `--effort <variant>`: the default is `high` (see 2c).
- `--check "<cmd>"`: may be repeated. It runs in the run's worktree root, or in a named repo with `<repo>:<cmd>`.
- `--setup "<cmd>"`: may be repeated, with the same scoping as `--check`. It runs before GLM starts, for example `pnpm install --frozen-lockfile --offline`.
- `--on-limit return|wait`: the default is `return`.
- `--max-waits N`
- `--timeout <seconds>`
- `--mode yolo|guarded|edit`: passed through to ZCode. The default is `yolo`.

#### 2a. Worktree isolation

For each `--repo`, the runner does the following:

- It runs `git worktree add <repo>-glm-<run-id> -b glm/<run-id> <base>`, where `<base>` is the repo's current `HEAD`.
- It records the base sha.
- If the caller's tree has uncommitted changes, the run reports them as a warning, because those changes are not in the worktree.
- It sets GLM's `--cwd`:
  - With one repo, `--cwd` is the worktree.
  - With several repos, `--cwd` is a workspace directory of links to each worktree. It is built the same way `build_workspace` builds one for the engine.

The brief is copied into the run directory and attached with `--attach`.

#### 2b. Rules: GLM is told, and they are enforced where possible

Every brief gets a fixed preamble. It says GLM must not:

- commit, push, stash, reset, rebase, merge or cherry-pick;
- switch, create or delete branches;
- edit outside the worktree(s);
- read or print `.env` files or keys.

**The `git` shim.** The runner puts a shim first on `PATH` for the ZCode process. The shim works like this:

- It parses the subcommand and its arguments.
- It refuses the forms listed below with the message `refused by countersign delegate: <reason>`. A refusal exits with a non-zero code.
- It passes everything else to the real git, found by searching `PATH` with the shim's own directory left out.

Refused:

- `commit`, `push`, `stash` (any form), `rebase`, `merge`, `cherry-pick`, `am`, `tag`, `worktree`.
- `switch`, in any form.
- `checkout` forms that change branch or `HEAD`:
  - `-b`, `-B` or `--orphan`;
  - a first non-option argument that is not followed by `--` and that resolves as a revision (`git rev-parse --verify --quiet <arg>^{commit}`) but is not an existing path in the worktree.
- `reset` with `--hard`, `--soft`, `--merge` or `--keep`, or with a revision argument before `--`.
- `branch` forms that create, delete, rename or copy a branch: `-d`, `-D`, `-m`, `-M`, `-c`, `-C`, `-f`, or a bare branch name.

Allowed and passed through:

- `checkout -- <paths>`, and `checkout <path>` when the path exists;
- `restore`, `add`, `rm`, `mv`;
- bare `reset`, or `reset [--] <paths>` (unstaging);
- `branch` with no arguments, or `branch --list` / `-a` / `-v`;
- `status`, `diff`, `log`, `show`, `grep`, and every other read-only command.

The shim is best-effort. An absolute call such as `/usr/bin/git` bypasses it, and the output contract says so.

**Checks after GLM exits.**

- The runner compares each worktree's `HEAD` with its base. Any commit shows up as `glm_committed: true`. The diff still covers that work, because it is computed from the base to the working tree (see 2g).
- The runner also lists any change to the caller's own tree during the run as a warning.
- It never reverts anything itself.

#### 2c. Model and effort configuration

The CLI has no model or effort flag. The settings come from two config files:

- The model comes from `model.main` in `~/.zcode/cli/config.json`.
- The effort comes from `provider.<p>.models.<model>.reasoning.defaultVariant` in `~/.zcode/v2/config.json`.

The values to apply are resolved in this order:

- **Model:** `--model`, then `delegate.model` in `~/.countersign/config.json`. If neither is set, the model key is left untouched.
- **Effort:** `--effort`, then `delegate.effort`, then the built-in default `high`. So the effort key is always set for the run, unless it already holds the resolved value.
- `--effort` and `delegate.effort` are validated against the variants the catalog lists for the chosen model. If the catalog does not list `high` for that model, the run is refused with a clear error instead of falling back silently.

How the values are applied, in order of preference:

1. **Private config copies.** If `ZCODE_*` config-path environment variables can point the CLI at a private copy of either file, the runner writes a per-run copy and nothing shared changes. The implementer checks this against ZCode 0.16.9 first and records the result in "As built".
2. **Edit the shared files under a lock.** If private copies are not possible, the runner sets only those two keys under an exclusive lock, `~/.countersign/zcode-config.lock`. It saves the exact prior values, and the values it wrote, next to the lock.

The runner restores the shared files in a `finally` block, and also on SIGINT and SIGTERM. For each key:

- The runner reads the key's current value from the file.
- If the value still equals what this run wrote, the runner restores the saved original.
- If the value differs, something else changed it during the run, such as the engine's `heal_zcode_cli_config` or the ZCode desktop app. In that case the runner does not overwrite it. It adds a loud warning to the output with the key, the value it found, and the original it skipped. The same warning also goes to stderr.
- This check lives only in `delegate.py`. The engine's behaviour does not change.

A run that finds a stale lock left by a dead PID takes it over. It then runs the same check-and-restore against the saved originals and saved written values before it applies its own settings.

The model and effort that actually served the run are read back with `zcode_model_desc` and reported in `model_served`.

#### 2d. Usage limits

**The usage monitor.** Before starting, the runner calls `GET https://api.z.ai/api/monitor/usage/quota/limit`, sending the key in the `Authorization` header.

- A bare key and `Bearer <key>` both work.
- The key is never logged, printed or written.
- The response shape was checked live on 2026-10-22. `data.limits[]` holds entries with `type`, `unit`, `number`, `percentage` (0–100) and `nextResetTime` (epoch ms).
- The windows:
  - `TOKENS_LIMIT` with unit 3, number 5 is the 5-hour window.
  - Unit 6, number 1 is the weekly window.
  - The response carries no token counts.
  - An unused window has no `nextResetTime`.

**Key source.**

- The key comes from the `ZAI_API_KEY` environment variable, or from a key file whose path is in `delegate.zai_key_file`.
- The key that `zcode_api_key()` finds in the ZCode desktop config is not used here. This endpoint refused it on 2026-10-22.
- With no key, the pre-check is skipped with a note, and limits are caught from the CLI's error.

**Pre-check.** A run is refused with outcome `rate-limited` when either of these is true:

- the 5-hour window's used share is at or above `delegate.max_start_share.five_hour` (default 0.6, because one task can use most of a window);
- the weekly window's used share is at or above `delegate.max_start_share.weekly` (default 0.9).

The refusal carries both reset times.

**A limit hit mid-run.** The runner detects it with `_looks_rate_limited` plus the ZCode wording "Usage limit reached for 5 hour". What happens next depends on `--on-limit`:

- `return` (the default): stop and report `rate-limited` with the session id and the reset time read from the monitor. `resume <run-id>` continues later. Whether to wait or spend a reset is the human's call.
- `wait`: sleep until the actual `nextResetTime` plus 3 minutes, never a fixed interval. Then continue with `--resume <sessionId>` and a short prompt: "you were interrupted; check the tree and finish". Stop after `--max-waits` waits (default 1).

**Quota report.** Both windows' shares are recorded before and after the run.

#### 2e. Checks

After GLM finishes:

- Each `--check` runs in its worktree, with a timeout.
- For each check, the output records the command, the exit code and the last 40 lines of its output.
- A failed check does not change the outcome. It is reported so the reviewer can decide.

#### 2f. Output contract (one JSON line)

```json
{"outcome": "done|rate-limited|failed|timeout|refused",
 "run_id": "...", "session_id": "sess_...",
 "repos": [{"repo": "...", "worktree": "...", "branch": "glm/...", "base": "<sha>",
            "files": [{"path": "...", "added": 12, "deleted": 3, "status": "M|A|D|R"}],
            "glm_committed": false}],
 "reply": "<GLM's final message, truncated to 4000 chars>",
 "checks": [{"cmd": "...", "repo": "...", "exit": 0, "tail": "..."}],
 "model_served": "GLM-5.3 (reasoning effort: high)",
 "quota": {"before": {...}, "after": {...}, "resets": {...}},
 "usage": {"input": 0, "output": 0, "cache_read": 0},
 "warnings": ["..."], "error": null, "duration_s": 0}
```

Notes on the fields:

- **`files`** includes files GLM created and never added, with `status: "A"` (see 2g).
- **`usage`** comes from the ZCode CLI's own JSON output for the run. It is extracted with the same usage-entry helper the engine uses, which moves to `zcode_common.py` in step 1. A resumed session's usage is added to the total.
  - If the CLI output carries no usage for delegate runs, each field is `null`. The implementer records this in "As built".
  - The z.ai monitor is never a source for this field, because it has no token counts.
- **`warnings`** always includes one fixed line: the git shim is best-effort.

Run state lives in `~/.countersign/delegate/<run-id>/`:

- the brief;
- the ZCode stdout and stderr;
- the report;
- the saved config originals and the values written;
- the `apply` record (see 2g).

`status` reads from there.

#### 2g. Diff, integration and cleanup

**Computing the diff.** Before the runner computes the file list (and again inside `apply` and `clean`), it runs `git add -A -N` (intent-to-add) in each worktree.

- This makes files GLM created but never added appear in `git diff <base>` as new-file entries.
- The diff is always `git diff --binary <base>`, from the base sha to the working tree. It covers committed, staged, unstaged and new files.
- New files that `.gitignore` excludes are not included. They are listed in `warnings` from `git status --porcelain --ignored`, limited to files created during the run, so the reviewer can see them.

**`apply <run-id>`** works on each repo in turn:

- It computes the diff as above.
- It refuses when the caller's tree has uncommitted or untracked changes in any of the same paths, unless `--force` is given.
- It runs `git apply --3way` into the caller's tree. The changes land uncommitted. New files arrive as untracked or intent-to-add files in the caller's tree.
- It records in the run directory the sha256 of the exact patch it applied, plus the list of paths.

The runner never commits to the caller's branch.

**`clean <run-id>`** works on each repo in turn:

- It runs `git add -A -N` in the worktree.
- It recomputes the full diff and compares its sha256 with the recorded `apply` patch.
  - If no `apply` record exists, the worktree has unapplied work whenever `git status --porcelain` shows any entry, untracked files included, or whenever `HEAD` differs from the base.
  - If the hashes differ, the worktree has unapplied work, for example edits made after `apply`.
- In either case, `clean` refuses unless `--force` is given, and names the unapplied paths.
- Otherwise it removes the worktree(s) and the `glm/<run-id>` branch(es).

### 3. `skills/delegate/SKILL.md`

The skill is model-invoked when the user asks to hand work to GLM. A session may also suggest delegating when a task fits the criteria. The skill covers the following.

**When to delegate:**

- implementing a section of a reviewed plan;
- tests from a spec;
- mechanical refactors and renames;
- i18n keys, scenario files and boilerplate, including new files.

**When not to delegate:**

- changes under about 50 lines;
- interactive debugging;
- anything that touches databases, credentials, git history or production;
- design decisions.

**The brief template:**

- Goal.
- Read first (exact paths).
- Do, as numbered steps.
- Do not.
- Checks to run.
- The exact final reply wanted. It must include "list any existing test you changed and why" and "list anything you decided that the brief did not say".

**Running it:**

- Run it as a background task.
- How to read the JSON and every outcome.
- `rate-limited` means: tell the human the reset time and offer `resume`. Never retry in a loop.
- The defaults are effort `high` and `--on-limit return`. Mention `--on-limit wait` only when the human has accepted an unattended wait.

**The review, which is mandatory:**

- read the diffstat, including the new files;
- read the risky files in full;
- re-run the checks yourself;
- spot-check how the new code connects to existing callers and flows;
- look for decisions GLM made silently;
- fix what you find;
- only then run `apply` and `clean`.

Never relay GLM's "all tests pass" as verified.

**Pairing with countersign:** a plan that reached consensus can be delegated section by section.

The README gains a short "Delegate" section, and the plugin version moves to 0.10.0.

### 4. Tests (stub ZCode, zero model spend)

Extend `tests/stub_zcode.py`. Add `tests/run_delegate_e2e.py` with its own temporary git repos.

- **Happy path.**
  - A stub edits a tracked file and creates a brand-new file in the worktree, without adding it.
  - The outcome is `done`.
  - The JSON line lists both files, with the new one as `status: "A"`.
  - The caller's tree is untouched.
- **Git shim, refused.** Each of these is refused with the clear message, and the worktree's branch and `HEAD` are unchanged:
  - `git commit`, `git push`, `git stash`;
  - `git checkout main`, `git checkout -b x`, `git switch y`;
  - `git reset --hard`;
  - `git branch -D x`, `git worktree add ...`.
- **Git shim, allowed.** These reach the real git and take effect:
  - `git checkout -- file`;
  - `git checkout file`, when the file exists;
  - `git restore file`;
  - `git add file`;
  - `git reset -- file`;
  - `git status`.
- **Git shim, bypass.** A real commit made through the absolute git path shows up as `glm_committed: true`, and its changes are still in the diff.
- **Pre-check.**
  - A recorded monitor response with the 5-hour window at 73% refuses the run, and the outcome carries the reset time.
  - With no key, the run proceeds with a note.
- **Mid-run limit, `return`.**
  - With no `--on-limit` given, a stub prints the ZCode usage-limit message.
  - The outcome is `rate-limited` with the session id.
  - `resume` continues the same session.
- **Mid-run limit, `wait`.** The sleep is computed from a recorded `nextResetTime`, not a fixed interval. The sleep is patched out.
- **Config restore.**
  - **Effort default:** with no effort configured, the effort is set to `high` during the run and the original is restored afterwards.
  - **`--model`:** the model is set and restored the same way.
  - **Stub raises, and SIGTERM:** the originals are restored in both cases.
  - **Stale lock:** a stale lock with a dead PID restores the saved originals.
  - **Concurrent change:** a key changed by another writer during the run is not overwritten, and a warning is emitted.
- **Usage.**
  - When the stub's JSON output carries usage, `usage` is filled in.
  - When it carries none, every field is `null`.
- **`apply`.**
  - The diff, including the brand-new file, lands uncommitted in the caller's tree.
  - An overlap with uncommitted caller changes is refused.
- **`clean`.**
  - It removes the worktree and branch after `apply`.
  - It refuses when the worktree holds only an untracked new file and `apply` never ran.
  - It refuses when the worktree was edited after `apply`.
  - `--force` overrides the refusal.
- **Engine.** `tests/run_e2e.py` still passes after the helper move.

### 5. Countersign: Claude or GLM implements, and the other model reviews

Today the engine's roles are fixed:

- **Plan mode:** GLM reviews; Claude revises.
- **`--implement`:** Claude implements.
- **`--verify`:** GLM reviews the diff, a forked Claude session fixes blocking objections, and the
  engine commits.

The skill's Stage 3 is always the interactive Claude session.

The engine gains two options, valid with `--implement` and `--verify`:

- **`--implementer claude|glm`.** The default is `claude`, which is today's behaviour. The default
  can also be set as `countersign.implementer` in `~/.countersign/config.json`.
- **`--verify-reviewer glm|claude`.** The default is the model that did not implement:
  - `glm` when Claude implemented (today's behaviour);
  - `claude` when GLM implemented.

  Passing the same model as the implementer is allowed but logs a warning: the point is a second
  opinion.

**When the implementer is `glm`:**

- **Implement.** The engine does not call Claude to implement. It runs `delegate.py run` with:
  - a brief built from the agreed plan or intent doc, using the same content
    `build_implement_prompt` produces today plus the delegate preamble;
  - each implement target repo as a `--repo`;
  - the commands from a new repeatable engine option, `--check "<cmd>"` (same `<repo>:<cmd>` scoping as
    delegate's), which the engine passes through unchanged. Without it, delegate runs no checks and
    the reviewer runs them.

  The usage pre-check and `--on-limit return` apply. A `rate-limited` delegate outcome becomes the
  engine's existing `rate-limited` outcome, with the delegate run id in the report so a re-run
  resumes it instead of starting over.
- **Verify.** It reviews the delegate run's worktree diff, base..working tree including new files
  (2g), not the caller's tree. The reviewer is Claude by default:
  - a headless `claude` call in a read-only permission mode;
  - the same verify review prompt and the same verdict JSON schema the GLM reviewer gets, so
    `parse_verdict`, the batching and the fix loop are unchanged.
- **Fix.** Blocking objections go back to the implementer: the engine resumes the GLM session with
  `delegate.py resume`, passing the objections as the prompt. The fix rounds, the round limit and
  the minors-never-block rule are as today.
- **Commit.** On a clean verify, the engine runs `delegate.py apply` into each repo's feature
  branch, then commits through its existing guarded commit step:
  - never on main/master;
  - the `countersign: verified` trailer;
  - minors in the commit message.

  Then it runs `delegate.py clean`. GLM itself still never commits.
- **Report.** `agent_models` records the implementer, the reviewer and the fixer, with the model and
  effort that actually served each.
- **Failure.** If `apply` fails, for example because of a conflict with caller changes made
  meanwhile, nothing is committed. The worktree is kept, and the outcome is `error` with the run
  id.

**When the implementer is `claude`:** nothing changes, except that `--verify-reviewer claude` is
now available, which makes Claude review Claude's diff and logs the warning above.

**The skill.** Stage 3 gains the choice. If the human says GLM should implement, or
`countersign.implementer` is `glm`, the session does not implement itself. It runs the engine with
`--implement --implementer glm` and then `--verify --implementer glm`. Plan mode (`/countersign`)
gains the same flags for its `--implement` run.

**Tests (stub agents, zero spend):**

- With `--implementer glm`, the stub ZCode implements in a worktree and the stub Claude reviews.
  The caller's tree is untouched until the clean verify, then the change lands and the commit
  carries the trailer.
- A blocking objection from the Claude reviewer reaches a resumed stub GLM session, and the fixed
  diff is re-reviewed.
- A `rate-limited` delegate outcome becomes engine outcome `rate-limited`, and a re-run resumes the
  same run id.
- `--implementer glm` on a repo on main/master is refused exactly as today.
- With no new flags, today's `tests/run_e2e.py` scenarios pass unchanged.

## Rollout

1. Move the helpers. The e2e test must stay green.
2. Write `delegate.py` with its tests.
3. Write the skill and the README section.
4. Add the implementer choice to the engine (section 5), with its tests. `tests/run_e2e.py` must stay green.
5. Do one live smoke run, with the owner's consent because it uses z.ai quota:
   - a trivial brief that creates one new file and edits one existing file, against a scratch repo;
   - record `model_served`, which should show effort `high`, plus `usage` and the quota before and after;
   - confirm that `apply` lands both files.

## As built

Implemented 2026-10-22 on `feature/glm-delegate` (plugin 0.10.0). All tests
green: `tests/run_e2e.py` (existing scenarios unchanged plus W1-W4) and the
new `tests/run_delegate_e2e.py` (D1-D11, M). Zero model spend - stub agents,
temp HOMEs, temp git repos, `file://` quota endpoints, patched-out waits.

### The ZCODE_* result (2c first preference)

**Private config copies are not possible; option 2 (shared files under a
lock) was implemented.** Read out of the CLI's own bundle
(`/opt/ZCode/resources/glm/zcode.cjs`, 0.16.9):

- `~/.zcode/cli/config.json` (the `model.main` pin) has **no env redirect**.
  Its path is hardcoded as `"~/.zcode/cli" + "config.json"` resolved against
  `os.homedir()`. (`ZCODE_STORAGE_DIR` exists but feeds other storage paths
  such as `cli/exec` and `clipboard`, not this file; `ZCODE_HOME` is
  telemetry-only. `os.homedir()` follows `$HOME` on POSIX, but repointing
  HOME for the CLI would also relocate `zcode login` credentials and rollout
  records - not a private-copy mechanism.)
- The v2 root **is** redirectable: `ZCODE_DATA_BASE_DIR` overrides the base
  under which `.zcode/v2/*` lives (personal catalog `provider_config.json`,
  `credentials.json`, telemetry), and `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` /
  `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` point directly at the two catalogs.
  But that only covers the catalogs/credentials - not the model pin's file -
  so a per-run private copy could never cover both keys anyway.

### Effort key correction

The plan's named effort key (`provider.<p>.models.<m>.reasoning.defaultVariant`
in `~/.zcode/v2/config.json`) is a **desktop-app** key: the string
`defaultVariant` appears nowhere in `zcode.cjs`; that file is the desktop
app's provider registry (`variants`/`defaultVariant` confirmed present in
the real file's key structure). The headless CLI's actual effort source is
`config.defaultModelSelection.options.reasoningLevel` in
`~/.zcode/v2/provider_config.json` (bundle: effective level =
`defaultModelSelection.options.reasoningLevel ?? <registry default>`, where
the registry default is the catalog model-rule's LAST listed level - GLM-5.3
lists `["low","high","max"]`, so unset means `max`). `delegate.py` therefore
writes **both** keys under the lock: the CLI-served
`defaultModelSelection` (providerId/modelId carried from the current
selection or pin, plus `options.reasoningLevel`), and the plan's named
`defaultVariant` in `~/.zcode/v2/config.json` (kept so the desktop TUI stays
consistent; skipped with a warning when no provider there lists the model).
`model_served` is still read back via `zcode_model_desc` (rollout record
ground truth), exactly as planned.

### Usage in the CLI's JSON output

The `--prompt --json` output carries a `usage` object - the engine has been
accumulating real reviewer-call usage from it since 0.9.x, and delegate runs
use the same output path, so `usage` is expected to be filled (the stub
tests cover both shapes). Not live-verified for an agentic `--mode yolo`
delegate run - the deferred smoke run (Rollout step 5) settles it; if such a
run carries no usage, every field is `null` per the plan.

### Differences from the plan

- **Effort write target** (above): CLI-served
  `config.defaultModelSelection.options.reasoningLevel` added alongside the
  plan's `defaultVariant` key, because the plan's key alone cannot move the
  headless CLI's effort.
- **Outcome `interrupted`** added beyond the plan's
  `done|rate-limited|failed|timeout|refused` enum for SIGINT/SIGTERM
  unwinding (mirrors the engine's contract); config originals are restored
  on that path too.
- **Setup failure stops the run before GLM** (outcome `failed`): running GLM
  against an environment whose setup failed would waste quota. The plan did
  not specify; checks (after GLM) never change the outcome exactly as
  planned.
- **Unprefixed `--check`/`--setup` run in EVERY worktree**; the plan said
  "the run's worktree root" (singular), which is ambiguous for multi-repo
  runs. `<repo>:<cmd>` scoping works as planned.
- **`apply` uses `git apply --3way`, which implies `--index`**: new files
  land staged (intent-to-add-like) rather than strictly untracked - within
  the plan's "untracked or intent-to-add". A real 3-way conflict still fails
  (outcome `failed`, nothing committed, worktree kept); `--force` only
  overrides the uncommitted-overlap refusal.
- **`clean` decides for every repo first, then removes** so a multi-repo
  refusal never leaves a partially cleaned run (plan was silent on this).
- **Effort validation source**: the desktop `v2/config.json`
  `reasoning.variants` first, else the app-shipped builtin catalog's
  modelRules (`optionSpecs.reasoningLevel.values`, most-specific regex match
  so the `glm-5.3` rule beats `glm-5` and `.*`). When neither catalog is
  readable the effort is accepted (cannot validate offline) rather than
  refused; refusal with the variants named happens exactly when a catalog
  lists the model without the requested variant.
- **Outcome `locked`**: a live holder of `~/.countersign/zcode-config.lock`
  refuses the run (the plan's stale-lock takeover is implemented as
  specified, including restoring the dead run's saved originals first).
- **Delegate zcode calls are single-attempt** (no exponential backoff): a
  spent 5h/weekly window is a limit, not a transient 429; the engine's
  backoff continues to cover its own reviewer calls.
- **Limit detection** covers both shapes: nonzero exit with rate-limit
  markers in stderr, and the CLI surfacing the spent window as the turn's
  final message (exit 0, session id still known - the shape the stub uses,
  so `rate-limited` reports carry the session id on a first-call limit).
- **Engine glm verify fallback**: if `--implementer glm` is given but no
  delegate run is recorded for the plan (e.g. the implement step ran as
  claude), verify reviews the caller's tree as usual with a logged note.
- The delegate brief **embeds the plan text** (GLM cannot read the caller's
  uncommitted plan file from its worktree) and adds the plan-section-3 "final
  reply" requirements; the engine passes `--check` through unchanged as
  planned.
- Minor: `status` also reports live worktree presence; the run dir keeps the
  brief, per-call zcode stdout/stderr, prompt, state, report and apply
  records as planned.

### Review fixes (Claude review, 2026-10-22)

- **Config file permissions.** `write_json_atomic` replaced files with a fresh temp file at the
  umask default, which widened ZCode's owner-only `~/.zcode/v2/provider_config.json` (0600) to
  0664. It now keeps the original file's mode. Test: D1 checks the file is still 0600 after a run.
- **`apply` no longer stages.** `git apply --3way` always writes the index, so a delegate diff
  landed staged and mixed with anything the human had staged. `apply` now tries a plain
  working-tree `git apply` first and falls back to `--3way` (with a warning that the changes are
  staged) only when the plain apply does not fit. Its apply record is now written even when a later
  repo fails, so `clean` does not treat already-applied repos as unapplied. Test: D10 checks the
  caller's index is empty after `apply`.
- **The engine commits only what was reviewed.** With `--implementer glm`, the verify commit ran
  `git add -A .` in the caller's repo, so the human's own uncommitted edits, which were never part of
  the reviewed worktree diff, would have been committed under the `countersign: verified` trailer.
  The commit now takes exactly the paths `delegate.py apply` reported (`git add`/`git commit --` on
  those paths), so other changes, staged or not, are left alone. Test: W1 writes an unrelated file
  in the caller's tree before verify and checks it is not in the commit.
- **The git shim refuses `git checkout -`** (switch to the previous branch). D2 covers it.
