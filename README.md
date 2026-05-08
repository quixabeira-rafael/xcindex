# xcindex

Blast-radius CLI for Xcode and SwiftPM projects. Queries the Xcode IndexStore
(built incrementally by Xcode/SwiftPM on every build) to answer "what is impacted
by changing this code?" with semantic precision — distinguishing definitions from
references, callers from callees, override chains, and cross-module reach.

## Why

Coding agents (Claude Code, Cursor, etc.) inspect change impact via `grep`. That
misses overrides, protocol conformance, indirect callers, and cross-layer reach.
The compiler already records all of this in the IndexStore. `xcindex` exposes it
as a small set of composable queries with output tuned for token economy.

## Install

```bash
pipx install git+https://github.com/quixabeira-rafael/xcindex
xcindex setup install            # builds the Swift helper (~60s, one-time)
xcindex doctor                   # validate environment
```

The helper is built lazily on first use if `setup install` is skipped.

## Quickstart

```bash
cd path/to/your-xcode-project
xcodebuild -scheme YourScheme build      # populate the IndexStore

xcindex search "OrderProcessor"          # find a symbol by name
xcindex symbol OrderProcessor            # full info on the matched symbol
xcindex at Sources/Order.swift:42        # what symbols are at this line?
xcindex containing Sources/Order.swift:42  # what method/class encloses this line?
xcindex reach <usr> --up --to-module UI    # who in UI uses this Core symbol?
```

First query in a project triggers a one-time dump of the IndexStore into a
content-addressed SQLite cache (`~/.cache/xcindex/<project>/<index-hash>.sqlite`).
Subsequent queries are sub-50ms cache hits. The cache invalidates automatically
when Xcode rebuilds (different hash → fresh dump).

## Commands

| Command         | Purpose                                                |
|-----------------|--------------------------------------------------------|
| `setup install` | Validate toolchain and build the Swift helper          |
| `setup uninstall` | Remove helper binary and SQLite caches               |
| `doctor`        | Health check (12 system + toolchain + project checks)  |
| `cache list`    | List cached SQLite files                               |
| `cache clear`   | Remove cached SQLite files                             |
| `search NAME`   | Substring search by symbol name (case-insensitive)     |
| `symbol USR\|NAME` | Look up a symbol's metadata                         |
| `at FILE:L[:C]` | List symbols/occurrences at a position                 |
| `containing FILE:L` | Find the enclosing symbol (method/class)           |
| `occurrences USR` | All occurrences of a symbol (filterable by `--role`)|
| `relations USR` | Relations of a symbol (`--direction in\|out`, `--kind`)|
| `neighbors USR` | 1-hop union of relations both directions               |
| `reach USR`     | Transitive closure (`--up\|--down`, `--depth`, `--to-module`) |

## Output engine (level × format)

Both dimensions are orthogonal and apply to every query.

**Levels** (each a strict superset of the previous):

| Level       | Includes                                                           |
|-------------|--------------------------------------------------------------------|
| `count`     | counts/booleans only (~30 tokens)                                  |
| `summary`   | counts + breakdowns by kind/module/depth (default; ~200 tokens)    |
| `locations` | + per-item: name, kind, module, file:line, container, role/depth   |
| `detailed`  | + USR, sub_kind, language, properties, rel_roles, full site info   |

**Formats**:

| Format     | Best for                                                  |
|------------|-----------------------------------------------------------|
| `agent`    | LLM context — markdown front-loaded, paths grouped (default) |
| `json`     | Programmatic / nested consumption                          |
| `jsonl`    | Streaming over large lists                                 |
| `compact`  | Shell pipelines (TSV: `file\tline\tname\tkind\tcontainer`) |

```bash
xcindex reach <usr> --up --to-module UI --level locations --format agent
xcindex search Order --format json --level detailed
```

## Recipes for agents

### Inspect a method-level change

You changed lines 42–47 inside `OrderProcessor.calculate(_:)`. To enumerate the
blast radius:

```bash
# 1. Identify the enclosing method
xcindex containing Sources/Order/OrderProcessor.swift:42 --format json

# 2. Get callers (who must be re-tested)
xcindex occurrences <usr> --role call --format json

# 3. Get override chain (who shares the contract)
xcindex relations <usr> --kind overrideOf --direction in    # who overrides this method
xcindex relations <usr> --kind overrideOf --direction out   # what does this override

# 4. Detect silent inheritors (subclasses that DON'T override)
xcindex relations <enclosing-class-usr> --kind baseOf --direction in  # subclasses
# subtract overriders from subclasses → silent inheritors
```

### Inspect a class-level change

```bash
# 1. All references to the type
xcindex occurrences <class-usr>

# 2. Subclasses + protocol conformers
xcindex relations <class-usr> --kind baseOf --direction in
xcindex relations <protocol-usr> --kind baseOf --direction out

# 3. Extensions
xcindex relations <class-usr> --kind extendedBy --direction in
```

### Cross-layer reach

```bash
# Does this Core change reach the UI layer?
xcindex reach <core-usr> --up --to-module YourAppUI --level summary

# Conversely, what does this UI symbol transitively use?
xcindex reach <ui-usr> --down --depth 6 --level locations
```

### Freshness

The IndexStore reflects the **last successful build**, not the current files on
disk. After editing source files, `xcindex` warns when results may be stale:

```bash
xcindex symbol Foo                  # warning appended if any source > index
xcindex symbol Foo --require-fresh  # exit code 4 (EXIT_STALE_INDEX) instead
```

The agent can then trigger a rebuild before relying on the result.

## Defaults (token economy)

- `--level summary` — front-loaded counts/breakdowns
- `--format agent` — markdown for LLM consumption
- `--limit 50` on list queries — caps output, sets `truncated:true` on overflow
- `--max-depth 8` for `reach`
- System SDK symbols excluded by default (override with `--include-system`)

## Performance

| Scenario           | Latency        |
|--------------------|----------------|
| Cache hit          | < 50ms         |
| Cache miss (small project) | ~1–5s    |
| Cache miss (large project) | ~10–30s  |
| `reach` warm       | < 200ms (typical) |

The cache is content-addressed by the IndexStore unit list + Swift version +
helper schema version, so concurrent builds and worktrees coexist cleanly.

## Limitations

- macOS only (depends on Xcode toolchain).
- Reflects the **last successful build**, not edits since.
- ObjC `@selector` / KVC / `NSClassFromString` are opaque (compiler doesn't index them).
- Closures stored and called indirectly are partially traced.
- Generated code (SwiftGen, R.swift, macros) is in the index but the source path
  may not exist on disk — `at`/`containing` emit a warning when this happens.
- Generic specialization fans out over all instantiations.

## Architecture

```
xcindex (Python CLI)
  ├─ subprocess + NDJSON streaming
  │       ↓
  ├─ xcindex-helper (Swift binary)
  │       └─ IndexStoreDB (Apple)
  │              └─ libIndexStore.dylib (in Xcode toolchain)
  │
  └─ SQLite cache (content-addressed by index hash)
```

Source of truth is always the IndexStore. The SQLite cache is materialized
on-the-fly and invalidated automatically when the IndexStore changes.

## Development

```bash
git clone https://github.com/quixabeira-rafael/xcindex
cd xcindex
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/unit -v             # fast, ~3s, 100+ tests
.venv/bin/pytest tests -v -m integration   # full suite, requires Xcode + Swift
```

Layout:
- `src/xcindex/` — Python package
- `swift-helper/` — SwiftPM package; produces `xcindex-helper` binary
- `tests/fixtures/SampleApp/` — minimal SwiftPM project for integration tests

## Exit codes

- `0` — success
- `1` — usage error (bad args)
- `2` — invalid state (no project found, etc.)
- `3` — system error (IO/permission)
- `4` — stale index (`--require-fresh` and source files newer than index)

## License

MIT.
