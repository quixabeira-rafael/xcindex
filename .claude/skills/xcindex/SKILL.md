---
name: xcindex
description: Query the Xcode IndexStore of a Swift/Objective-C project for symbols, references, and reachability using the xcindex CLI. Use when the user asks to find a symbol, locate where a class/method is used, identify the blast radius of a change, list call sites or subclasses, find what's at file:line, ask "what depends on this", or any "who-uses-X / what-does-X-call" question on an Xcode project. Trigger phrases include "blast radius", "find references", "who calls", "callers of", "where is this used", "what does this depend on", "subclasses of", "implementations of", "find symbol", "what's at file:line", "what method contains this line", or any reference to IndexStore / IndexStoreDB / SourceKit indexing on an Xcode/SwiftPM project.
---

# xcindex

CLI that materializes Xcode's IndexStore into a per-project SQLite cache and answers symbol/reference/reachability queries against it. Designed to be operated by an agent: every subcommand emits a stable JSON shape with `--json`.

## Mental model (read first)

Seven facts that are not obvious from `--help`:

1. **xcindex queries the IndexStore, never the source code.** The compiler writes the IndexStore as a side effect of every build. If the project hasn't been rebuilt since a code change, queries reflect the *previous* build state. Use `--check-fresh` to surface a warning when source files are newer than the latest unit; use `--require-fresh` to fail with exit 4 (`EXIT_STALE_INDEX`).

2. **First query bootstraps a SQLite cache.** The Swift helper walks the IndexStore via the libIndexStore C API and writes `~/.cache/xcindex/<project_fingerprint>/index.sqlite`. Cold dump on a large iOS workspace is ~30s; subsequent queries are <500ms. The cache is keyed by absolute project path.

3. **Tuist regen forces a full re-bootstrap.** `tuist generate` rewrites the IndexStore directory; xcindex's unit delta detector sees every unit as "added" and falls back to a fresh cold dump. On Tuist projects this happens daily, not once.

4. **Cache invalidation is per-unit, mtime + size.** Edit one Swift file, rebuild, and the next query runs an incremental update (~1s) — only that file's symbols/occurrences/relations are deleted and re-inserted. New units fall back to full re-bootstrap (we can't infer their files yet); removed units are cleaned up in place.

5. **System symbols are excluded by default.** UIKit, Foundation, SwiftUI, and SDK frameworks are filtered unless you pass `--include-system`. Including them bloats the cache, slows the cold dump, and drowns project-symbol queries in SDK noise. Reach for `--include-system` only when the question is explicitly about an SDK type.

6. **USRs are the canonical handle.** Plain names collide (overloads, the same name in different modules, extension symbols vs the type they extend). The IndexStore identifies symbols by USR (`s:11ModuleName10TypeNameC...`). Most commands take a USR. Resolve name → USR with `xcindex symbol --name X --json` or `xcindex search X --json`, then drive the rest of the workflow off the USR.

7. **JSON is the agent contract.** Every subcommand accepts `--json`. Stable shape: `{kind, anchor, summary, items, truncated, warnings}`. `summary.found` and `summary.count` are always present. `items` may be elided when `--level count`. Errors in JSON mode go to stderr as `{"error": "<code>", "message": "..."}`. Use `--json` when parsing programmatically.

## When to invoke this skill

- "What's at `Foo.swift:42`?" → `xcindex at Foo.swift:42`
- "What method/class contains `Foo.swift:42`?" → `xcindex containing Foo.swift:42`
- "Find a symbol named `PriceCalculator`" → `xcindex search PriceCalculator` or `xcindex symbol --name PriceCalculator`
- "Where is `Money` used?" → `xcindex symbol --name Money` to get USR, then `xcindex occurrences <usr>`
- "Who calls `applyDiscount`?" → `xcindex reach <usr> --direction up --kinds calledBy`
- "What does `processCheckout` depend on?" → `xcindex reach <usr> --direction down`
- "Subclasses of `BaseViewController`" → `xcindex relations <usr> --direction in --kind baseOf`
- "Why is xcindex broken / where's the cache?" → `xcindex doctor`, `xcindex cache list`

## Workflow A — first-time setup

```bash
xcindex setup install        # one-time: builds the Swift helper (~60s)
xcindex doctor               # sanity check: macOS, Swift, project discovery, cache dir
cd /path/to/your/Xcode/project
xcodebuild -workspace ... -scheme ... build   # OR build in Xcode
# (the build is what produces the IndexStore xcindex queries)
xcindex search SomeSymbol    # first query: bootstraps the cache (~10–30s)
```

`setup install` is idempotent. After a `pipx reinstall xcindex` it auto-rebuilds the helper if the schema bumped. If the user ran `pipx install -e .` from the repo, `xcindex skill install` is also offered during setup install (when Claude Code is detected on the machine).

## Workflow B — locate a symbol

```bash
# Substring match across all project symbols (NOCASE)
xcindex search Receipt --kind class --json

# Exact name (returns all overloads / homonyms)
xcindex symbol --name PriceCalculator --json

# Already have a USR (from a previous query)
xcindex symbol s:11ModuleName10TypeNameC --json

# Symbol at a cursor position
xcindex at Sources/Money.swift:14:8 --json

# Enclosing symbol for a line (no column needed)
xcindex containing Sources/Money.swift:42 --json
```

`--kind` and `--module` filters apply to `search`. `--limit` defaults to 50; results carry `truncated: true` when the cap is hit.

## Workflow C — find references

```bash
# All occurrences of a symbol (definition + references + calls + writes…)
xcindex occurrences <usr> --json

# Filter to one role (declaration, definition, reference, read, write, call, dynamic, addressOf, implicit)
xcindex occurrences <usr> --role call --json
xcindex occurrences <usr> --role write --json

# 1-hop graph around the symbol (relations in/out without filtering)
xcindex neighbors <usr> --json
xcindex neighbors <usr> --kind calledBy --direction in --json
```

`relations` is the lower-level form: `xcindex relations <usr> --direction in --kind baseOf` is "who inherits from this" while `--direction out --kind calledBy` is "what does this call". `neighbors` is the union of both directions.

## Workflow D — blast radius (the headline use case)

```bash
# Who transitively uses this symbol? (callers of callers, …)
xcindex reach <usr> --direction up --max-depth 8 --json

# What does this symbol transitively use?
xcindex reach <usr> --direction down --max-depth 8 --json

# Limit traversal to specific relation kinds
xcindex reach <usr> --direction up --kinds calledBy,overrideOf --json

# Constrain output to a single module (still traverses through any module)
xcindex reach <usr> --direction up --to-module FeatureCheckout --json
```

Default traversal kinds: `calledBy, containedBy, childOf, overrideOf, baseOf, specializationOf, extendedBy`. The `summary.by_module` and `summary.by_depth` breakdowns are usually more useful than the raw `items` list when reporting a blast radius back to the user.

## Workflow E — after editing source

```bash
# Edit Money.swift in your editor
xcodebuild ... build           # rebuild → updates the IndexStore unit for Money.swift
xcindex search Money --json    # automatic incremental update (~1s) before the query
```

xcindex compares each unit's `(size, mtime)` against the cache before answering; modified units trigger an incremental DELETE+INSERT scoped to those files. No manual invalidation needed. To verify the cache is fresh against source files, add `--check-fresh` (warning) or `--require-fresh` (hard fail with exit 4).

## Workflow F — clean / inspect cache

```bash
xcindex cache list --json                   # all caches across all projects
xcindex cache list --project . --json       # caches for the current project
xcindex cache clear                         # nuke the active project's cache (forces re-bootstrap)
xcindex cache clear --all                   # nuke ~/.cache/xcindex entirely
```

Each project's cache directory holds the live `index.sqlite` plus up to 3 `legacy_*.sqlite` snapshots (preserved across schema bumps for forensics; trimmed via GC).

## Command reference

| Command | Purpose | Key flags |
|---|---|---|
| `at FILE:LINE[:COL]` | Symbols at a file position | resolves to one or many occurrences |
| `containing FILE:LINE` | Smallest enclosing symbol for a line | heuristic: largest symbol-line ≤ target line |
| `symbol {USR \| --name NAME}` | Look up symbol details | returns all matches when --name (overloads) |
| `search PATTERN` | Substring (NOCASE) on symbol names | `--kind`, `--module`, `--limit` |
| `occurrences USR` | All occurrences of a symbol | `--role` (one of declaration/definition/reference/read/write/call/dynamic/addressOf/implicit) |
| `relations USR` | Relations involving the symbol | `--direction in/out`, `--kind` (calledBy, baseOf, …) |
| `neighbors USR` | 1-hop union of in+out relations | `--direction both/in/out`, `--kind` |
| `reach USR` | Transitive closure via recursive CTE | `--direction up/down`, `--max-depth N`, `--kinds k1,k2`, `--to-module M` |
| `cache {list,clear}` | Inspect or nuke the on-disk cache | `--all` for `clear`; `--project` for `list` |
| `doctor` | Health check (macOS, Swift, project, IndexStore, cache, helper) | exits 0/2/3 by severity; prints `fix:` hints |
| `setup {install,uninstall}` | Build the Swift helper, prepare dirs | `--with-skill` / `--skip-skill` for the Claude skill prompt |
| `skill {install,uninstall,status}` | Install/remove the Claude Code skill at user level | symlinks `~/.claude/skills/xcindex/SKILL.md` |

Every subcommand accepts `--json`. Project-scoped commands (everything except `setup`, `doctor`, `cache list --all`) also accept `--project PATH`, `--index-store PATH`, `--derived-data PATH`, `--include-system`, `--check-fresh`, `--require-fresh`.

Output projection (for the non-JSON renderers): `--level count|summary|locations|detailed`, `--format agent|json|jsonl|compact`. `--format json` is equivalent to `--json`.

Exit codes: `0` success, `1` usage error, `2` invalid state, `3` system failure, `4` stale index (only when `--require-fresh`).

## Roles and relation kinds

**Occurrence roles** (bitmask in `roles`, decoded in `summary.by_role` and `items[].roles`):
`declaration, definition, reference, read, write, call, dynamic, addressOf, implicit`.

**Relation kinds** (in `r.kind`):
`childOf, baseOf, overrideOf, receivedBy, calledBy, extendedBy, accessorOf, containedBy, ibTypeOf, specializationOf`. `other` for unclassified.

`reach` defaults to traversing `calledBy, containedBy, childOf, overrideOf, baseOf, specializationOf, extendedBy` — call graphs and inheritance chains.

## When something is broken

Run `xcindex doctor` first. It runs a fixed checklist (macOS version, Python version, xcrun, Swift toolchain, pipx, cache dir, helper presence, helper version match, project discovery, IndexStore freshness) and prints a `fix:` hint for each non-OK row. Beats guessing.

## Gotchas checklist

- **`could not discover project`** → the cwd doesn't contain `.xcodeproj`/`.xcworkspace`/`Package.swift`. `cd` to the project root or pass `--project /path/to/project`.
- **`could not discover index store`** → the project hasn't been built. Build in Xcode (or `xcodebuild build`) to populate the IndexStore at `<DerivedData>/<project>-<hash>/Index.noindex/DataStore`.
- **First query is "stuck" for 30 seconds** → that's the cold dump (helper bootstrapping the SQLite cache). It only happens once per IndexStore content. Don't kill it.
- **Cold dump happens *every* time** → likely a Tuist project regenerating the IndexStore. Expected. The dump is ~15–30s on large workspaces; nothing else is wasted, the cache is reused once stable.
- **`schema upgraded to v3; preserved N legacy snapshot(s)`** → harmless; the schema version bumped, the old SQLite was kept for forensics under `legacy_*.sqlite` and a fresh one was bootstrapped. No data lost.
- **Searches for `UIView`, `UITableView`, etc. return 0 hits** → SDK symbols are filtered by default. Re-run with `--include-system`. Cache rebuilds with system symbols are larger and slower; consider whether the question really requires it.
- **`reach` returns nothing for a symbol that obviously has callers** → check `--max-depth` (default 8) and `--kinds`. The default kind set excludes `accessorOf` and `receivedBy`; pass `--kinds calledBy` explicitly when you only care about the call graph.
- **`occurrences --role definition` returns 0** → the symbol is declared but never defined in the project (e.g., a protocol requirement, an Obj-C method bridged from a header). Try without `--role`, then read the role bitmask on each item.
- **A USR you saved a week ago no longer resolves** → IndexStore USRs are stable across builds for the same Swift compiler version, but a Swift toolchain upgrade can change them. Re-run `search` to find the new USR.
- **`--check-fresh` is slow on large repos** → it walks the project tree. Default is OFF for that reason. Only opt in when freshness matters for the question being asked.
- **Helper schema mismatch loop** → `xcindex setup install` rebuilds the helper. If `xcindex doctor` shows `helper-version: warn (schema X, expected Y)`, run setup install.

## JSON-mode contract

Every command emits a single JSON object on stdout with `--json`. Stable top-level keys:

- `kind` — query family (`at`, `containing`, `symbol`, `occurrences`, `relations`, `neighbors`, `reach`, `search`, …).
- `anchor` — what was queried (USR, name, file:line, pattern, …).
- `summary` — counts and breakdowns (`found`, `count`, `files`, `by_role`, `by_kind`, `by_module`, `by_depth`, …).
- `items` — per-result rows (omitted when `--level count`). Each row carries `name`, `usr`, `kind`, `module`, `language`, `file`, `line`, `column`, plus query-specific fields (`roles`, `container`, `rel_kind`, `rel_roles`, `site`, `depth`).
- `truncated` — boolean; true when results were capped by `--limit`.
- `warnings` — list of strings; staleness warnings appear here when `--check-fresh` is set.

Errors emit `{"error": "<code>", "message": "..."}` on stderr (still single-line JSON). Exit codes are normative.

## Performance expectations

- **Cold dump** (first query after `tuist generate` or initial setup): 5–30s depending on project size.
- **Hot cache hit**: <500ms for any single-symbol query, including `reach`.
- **Incremental update** (one Swift file edited, rebuilt): ~1s before the next query answers.
- **`reach` with default depth/kinds**: <500ms even on workspaces with 500k+ occurrences.

If a query takes longer than expected with no IndexStore changes, run `xcindex doctor` — most "slow" reports are actually cold dumps masquerading as slow queries.
