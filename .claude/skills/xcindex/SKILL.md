---
name: xcindex
description: Query the Xcode IndexStore of a Swift/Objective-C project for branch-level impact, blast radius, file inventory, and symbol search using the xcindex CLI. Use when the user asks "what does this branch touch and what breaks if I ship it", "what's the impact of changing this method/class", "list the types in this file", or "find a symbol named X" on an Xcode / SwiftPM project. Trigger phrases include "blast radius", "impact of this change", "what does this branch touch", "what would break if I change X", "who calls", "callers of", "subclasses of", "find symbol named", "types in this file", "what's in this Swift file", or any reference to IndexStore / IndexStoreDB on an Xcode workspace.
---

# xcindex

CLI that materializes Xcode's IndexStore into a per-project SQLite cache and answers symbol/reference/reachability/impact queries against it. Designed to be operated by an agent: every subcommand emits a stable JSON shape with `--format json`, and a stack-frame-style markdown shape by default (`--format agent`).

## What this skill covers

The four daily-driver commands have rich workflows below. Everything else is a primitive — invoke the relevant `--help` and read the output:

| Command | Day-to-day use? | Where to learn |
|---|---|---|
| **`git`** | ✅ start every session | this skill, "Workflow A" |
| **`impact`** | ✅ drill into one symbol | this skill, "Workflow B" |
| **`file`** | ✅ inventory a file | this skill, "Workflow C" |
| **`search`** | ✅ locate a symbol by name | this skill, "Workflow D" |
| `at`, `containing`, `symbol`, `occurrences`, `relations`, `neighbors`, `reach` | primitives | run `xcindex <cmd> --help` |
| `prewarm` | wire into build hooks to keep cache warm | this skill, "Workflow E" |
| `watch` | foreground watcher that auto-runs prewarm on builds | this skill, "Workflow E" |
| `cache`, `doctor`, `setup`, `skill` | infra | run `xcindex <cmd> --help` |

If the question maps cleanly to one of the four primary commands, use it. The primitives still exist for power-user queries (e.g. role-filtered occurrences, hand-tuned reach traversals) — fall through to `--help` when the daily commands aren't enough.

## Mental model (read once)

Six facts that change how you should call xcindex:

1. **xcindex queries the IndexStore, never the live source.** The compiler writes the IndexStore as a side effect of every build. If the project hasn't been rebuilt since a code change, queries reflect the *previous* build state. After editing source, rebuild before drawing conclusions. Add `--check-fresh` to surface a warning, or `--require-fresh` to fail with exit 4.

2. **First query bootstraps a per-project SQLite cache.** Cold dump on a large iOS workspace is ~15–30s; subsequent queries are <500ms. The cache lives at `~/.cache/xcindex/<project_fingerprint>/index.sqlite` and is keyed by absolute project path. Don't kill the first query — it's not stuck, it's bootstrapping. To pay this cost in the background instead, see `xcindex prewarm` (Workflow E).

3. **Cache invalidation is per-unit, mtime + size.** Edit one file, rebuild, the next query runs an incremental update (~1s). New source files fall back to a full re-bootstrap. Tuist `tuist generate` rewrites every unit, so it forces a cold dump too.

4. **System symbols are excluded by default.** UIKit/Foundation/SwiftUI noise stays out unless you pass `--include-system`. Reach for `--include-system` only when the question is explicitly about an SDK type.

5. **USRs are the canonical handle.** Names collide; USRs (`s:11Module10TypeNameC...`, `c:@M@Module@objc(cs)Foo`) don't. Most commands take a USR. Resolve names to USRs once via `search`/`file`/`git`, then drive the rest of the workflow off the returned USR. ObjC USRs contain `()` so always wrap them in single quotes when pasting into a shell: `xcindex impact 'c:@M@Foo@objc(cs)Bar'` — xcindex's own next-step output already does this.

6. **Output format defaults are tuned for agents.** Default is markdown with stack frames / tables / next-step suggestions. Pass `--format json` for programmatic parsing; the JSON shape is stable: `{kind, mode?, anchor, summary, items|stacks|files, truncated, warnings}`.

## Workflow A — what does this branch touch? (`xcindex git`)

The most common question at the start of a review session: "I have a branch open — what symbols did it touch and what breaks if I ship it?" `xcindex git` answers this in one shot.

```bash
# default base (origin/main → main → HEAD~1) — entire branch's diff vs main
xcindex git

# explicit base
xcindex git main
xcindex git HEAD~3
xcindex git release/2025.10

# only staged changes (pre-commit workflow)
xcindex git --staged
```

Output structure:
- **One block per modified Swift/ObjC file**, listing the modified line range, the enclosing symbol's name, kind, and USR (one line per symbol, deduped by USR).
- **`**next steps**` block** with copy-paste commands:
  - `xcindex file <path>` for each modified file
  - `xcindex impact <usr>` for each modified symbol
- **Warnings** for added files (not yet indexed → rebuild) and deleted/renamed files.

Decision-making after running `git`:
- Few symbols touched → run `xcindex impact <usr>` on each one to assess blast radius.
- Many symbols touched → look at the file list first; ones with the most-touched core symbols are the riskiest. Drill into those first.
- Added file warning → ask the user to rebuild before continuing.

```bash
# typical session
xcindex git                                            # see what the branch did
xcindex impact 's:6WWCore11AuthManagerC...refreshyyF'  # drill into a flagged symbol
xcindex file 'Sources/Core/OrderProcessor.swift'       # cross-check what other types live nearby
```

## Workflow B — blast radius of one symbol (`xcindex impact`)

`xcindex impact` is the headline query. It produces **bidirectional call/usage stacks** in stack-frame format (like an Xcode debugger): a list of independent paths from each terminal caller up to the target, plus paths from the target down to each terminal callee.

Three modes, dispatched by the target's kind:

| Target kind | Mode | What you get |
|---|---|---|
| `instance-method`, `class-method`, `static-method`, `function`, `constructor`, `destructor` | **call_stack** | upstream stacks (callers via `calledBy` + `overrideOf`) and downstream stacks (callees via `calledBy` inverted) |
| `class`, `struct`, `enum`, `protocol` | **usage_chain** | upstream usage chains (level 1 = reference containers, then BFS via callers) + a flat structure block (members, subclasses/conformers, extensions) |
| anything else (property, extension, typealias, parameter, enum-case, …) | **hint_only** | no stack walk; emits 3-5 kind-appropriate `xcindex` follow-up commands |

Input forms (same flexibility as `xcindex file`):

```bash
# By file:line — resolves to the enclosing symbol
xcindex impact Sources/Core/AuthManager.swift:42

# By name — errors with shell-safe candidates if ambiguous
xcindex impact 'attemptLogin(_:)'

# By USR (Swift)
xcindex impact 's:6WWCore11AuthManagerC...attemptLoginyyF'

# By USR (ObjC) — quote because of the parens
xcindex impact 'c:@M@WWMobile@objc(cs)AppDelegate(im)init'
```

Tuning flags:

```bash
# Default depth=8, max-stacks=10 per direction. Bump for highly-connected symbols:
xcindex impact <usr> --depth 12 --max-stacks 25

# Restrict to where impact lands in a specific module
xcindex impact <usr> --to-module WWMobileUI

# Strict call-only (skip overrideOf — useful if you're not changing the signature)
xcindex impact <usr> --no-overrides

# One direction only
xcindex impact <usr> --up-only       # who calls me
xcindex impact <usr> --down-only     # what do I call
```

Reading the output:
- Each `[upstream stack N] depth K (edge_kinds)` is one independent path. Read top-to-bottom: row 0 is the entry point (test, scene root, etc.), last row is the target.
- Edge kinds in the header tell you what kind of edge connects the frames (e.g. `(overrideOf)` means at least one frame is an override, not a direct call).
- `(external)` frames are SDK / Foundation / XCTest internals — no file:line because they're outside the indexed code.
- `**summary**` block: `transitive_count`, `module_count`, `by_module`, `by_depth`, `by_edge_kind`. Use these to answer "how big is the blast radius" without reading every stack.

For type targets the **`**structure**`** block lists members/subclasses/extensions — call `xcindex impact` again on a specific member to drill in.

## Workflow C — what types are in this file? (`xcindex file`)

Inventory a file before refactoring, or expand on a `git` finding. Default output is a table of top-level types (class/struct/enum/protocol); `--all` widens to every definition (methods, properties, extensions, parameters).

```bash
# Shorthand: `xcindex <file>` → same as `xcindex file <file>`
xcindex Sources/Core/AuthManager.swift
xcindex AuthManager.swift            # filename only
xcindex AuthManager                  # bare stem (any extension)

# Explicit
xcindex file Sources/Core/AuthManager.swift

# Everything in the file, not just top-level types
xcindex file Sources/Core/AuthManager.swift --all --limit 200
```

Multiple files matching the same basename → error with a copy-paste-ready list of full paths (one `xcindex` invocation per match). Common in iOS workspaces with many `AppDelegate.swift`.

The output table has **kind / name / USR** columns. Use the USRs to follow up with `xcindex impact <usr>`. The default output also includes a **`**next steps**`** block with kind-aware suggestions (subclasses, extensions, members for types; callers, override chain for methods; reads/writes for properties).

## Workflow D — find a symbol by name (`xcindex search`)

Substring search (case-insensitive) when you know roughly what you're looking for but not the exact USR or location.

```bash
xcindex search PriceCalculator
xcindex search Receipt --kind class --limit 20
xcindex search login --kind instance-method --module WWCore
xcindex search Order --format json --level detailed   # programmatic
```

Filters:
- `--kind` — narrow by kind (`class`, `struct`, `instance-method`, etc.)
- `--module` — narrow to one Swift module
- `--limit` — default 50, hard-cap with `truncated: true` flag

`search` is the right entry when:
- The user described a symbol by name and you need its USR.
- You're hunting for naming patterns (`xcindex search ViewController --kind class`).
- You need a population estimate ("how many `setUp` methods exist?").

When the user gives an exact name and there's likely one match, prefer `xcindex symbol --name <name>` (use `xcindex symbol --help`) — it returns immediately without substring noise.

## Workflow E — keep the cache warm (`xcindex prewarm`)

The cold dump (~15–30s) is paid lazily on the first query unless you trigger it explicitly. `xcindex prewarm` runs the same dispatch (cold / incremental / noop) as a one-shot command; wiring it into a build hook eliminates user-visible cold start.

```bash
xcindex prewarm                # default text output
xcindex prewarm --quiet        # silent on noop (good for hooks)
xcindex prewarm --format json  # programmatic
```

Modes the command may report:
- **cold** — first run or new units detected (Tuist regen, new source file).
- **incremental** — units modified since last call (~1s).
- **noop** — cache already in sync.
- **schema_upgrade** — helper bumped schema; full re-bootstrap.

Always exits 0 on success. `--no-build-helper` makes it fail fast if the helper binary is missing instead of rebuilding (useful when called inside a build phase where the rebuild cost is unwanted).

Doctor reports whether the cache is in sync via the `cache-warm` check (`OK` / `WARN: N units stale` / `INFO: no cache yet`).

The materialization function is a public Python API too:
```python
from xcindex import materialize, MaterializationResult
result = materialize(args)
print(result.mode, result.wall_seconds, result.symbols_added)
```

**Hook option 1 — shell alias** (CLI builds only, simplest setup):
```bash
# ~/.zshrc
xcb_with_prewarm() { command xcodebuild "$@" && command xcindex prewarm --quiet 2>/dev/null; }
alias xcodebuild=xcb_with_prewarm
```
Misses builds from the Xcode IDE.

**Hook option 2 — `xcindex watch`** (covers IDE + CLI builds, ~30MB resident):
```bash
xcindex watch                  # foreground; Ctrl+C to stop
xcindex watch --debounce 1000  # wait 1s after last event before prewarming
```
Subscribes to FSEvents on the IndexStore, debounces bursts, spawns `prewarm` per settled event. Single-instance per project. Resilient — keeps running even when individual prewarms fail.

Watcher status is reported by `xcindex doctor`:
- `[OK] watcher running (pid=…, since…, last prewarm: incremental 0.7s)` — healthy
- `[!!] watcher running (...) — 3/4 prewarms failed` — escalates to WARN
- `[--] no watcher running` — info, with `xcindex watch` as fix hint
- `[XX] stale state file (pid X not running)` — error, auto-cleaned on next start

When the user asks for "auto" cache warming and is fine with a foreground process, recommend Hook 2. When they only build from CLI and don't want a long-running process, recommend Hook 1. Project-aware auto-installation (Tuist plugin, etc.) is deferred to the planned `xcindex profile` feature.

## Everything else: read `--help`

These primitives back the headline commands and are still useful for power-user queries — but the daily flow rarely needs them directly:

```bash
xcindex at --help            # symbols at file:line[:column]
xcindex containing --help    # smallest enclosing symbol for a line
xcindex symbol --help        # exact USR/name lookup
xcindex occurrences --help   # all occurrences of a USR (filterable by --role)
xcindex relations --help     # 1-hop relations (--direction in/out, --kind X)
xcindex neighbors --help     # union of in+out 1-hop relations
xcindex reach --help         # transitive closure (raw, flat output — `impact` is usually better)
xcindex cache --help         # inspect/clear the SQLite cache
xcindex doctor --help        # health check (run when something feels wrong)
xcindex setup --help         # build/reinstall the Swift helper
xcindex skill --help         # install/uninstall this Claude Code skill
```

Rule of thumb: if you reach for `relations` / `reach` / `occurrences`, ask whether `impact` answers the same question with better defaults. If yes, prefer `impact`.

## JSON contract

Every command accepts `--format json` (or `--json` on `doctor`). Stable top-level keys:

- `kind` — query family (`git`, `impact`, `file`, `search`, …)
- `mode` — present on `impact` only (`call_stack` / `usage_chain` / `hint_only`)
- `anchor` — what was queried (USR, file:line, base ref, …)
- `summary` — counts and breakdowns (`found`, `count`, `by_kind`, `by_module`, `by_depth`, …)
- `items` / `stacks` / `files` — depending on `kind` (mutually exclusive — read the kind first to know which key to expect)
- `truncated` — `true` when results were capped
- `warnings` — list of strings (staleness, indexing notes)

Errors emit `{"error": "<code>", "message": "..."}` on stderr. Exit codes are normative:
- `0` success
- `1` usage error
- `2` invalid state (project not found, target not in index, ambiguous name, …)
- `3` system error (IO/permissions)
- `4` stale index (only with `--require-fresh`)

## Output projection

Two orthogonal axes apply to every command:

- `--level count|summary|locations|detailed` — how much detail per item (default varies by command; `impact` defaults to `locations`, others to `summary`).
- `--format agent|json|jsonl|compact` — agent (markdown, default), json (single object), jsonl (header + 1 line per item), compact (TSV).

For agent consumption, defaults are tuned. Override only when piping into another tool.

## When something feels wrong: `xcindex doctor`

```bash
xcindex doctor             # human-readable
xcindex doctor --json      # programmatic
```

Runs a fixed checklist (macOS, Python, xcrun, Swift, pipx, cache dir, helper presence, helper schema, project discovery, IndexStore freshness, **git working tree**) and prints a `fix:` hint per non-OK row. Beats guessing.

If the user reports xcindex "not working" or commands erroring with project/index/cache complaints — start with `xcindex doctor`.

## Gotchas you'll hit

- **`could not discover project`** → cwd has no `.xcodeproj`/`.xcworkspace`/`Package.swift`. cd to the project root or pass `--project /path/to/project`.
- **`could not discover index store`** → project hasn't been built. Tell the user to run `xcodebuild -workspace ... -scheme ... build` (or build in Xcode).
- **`xcindex git` says "no indexable file changes detected"** → the diff hit only non-Swift files (Podfile, .yml, etc.) — that's fine, just nothing for xcindex to do.
- **`xcindex git` flags an added file** → the IndexStore won't have it until next build. Ask the user to rebuild before drilling into that file.
- **First query is "stuck" for 30 seconds** → cold dump. Wait it out. Only happens once per IndexStore content.
- **Cold dump every time** → likely a Tuist project regenerating the IndexStore. Expected; nothing wasted.
- **`xcindex search UIView` returns 0** → SDK symbols filtered. Re-run with `--include-system` if the question is genuinely about an SDK type.
- **`xcindex impact` returns "no transitive callers/callees found"** → the symbol is genuinely a leaf entry-point, OR the cache is stale. If the user just edited the file, ask them to rebuild.
- **ObjC USR copy-paste fails in zsh with `nomatch`** → quote it. The skill / CLI output already does this; if you constructed a USR by hand, wrap in `'...'`.
- **A USR you saved a week ago no longer resolves** → IndexStore USRs are stable across builds for the same Swift version, but a Swift toolchain upgrade changes them. Re-run `search`.
- **Helper schema mismatch** → `xcindex doctor` will show `helper-version: warn`. Run `xcindex setup install` to rebuild.

## Performance expectations

- **Cold dump** (first query / after `tuist generate`): 5–30s.
- **Hot cache hit**: <500ms for any single-symbol query.
- **Incremental update** (one file edited, rebuilt): ~1s before the next query.
- **`xcindex impact` on a deeply-connected symbol**: <2s on workspaces with 500k+ occurrences.
- **`xcindex git` on a 100-file branch**: <2s including hunk parsing and per-line `containing` resolution.

If a query takes longer than expected with no IndexStore changes, run `xcindex doctor` — most "slow" reports are cold dumps.
