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
| `file FILE`     | List type definitions in a source file (also: `xcindex <file>`) |
| `occurrences USR` | All occurrences of a symbol (filterable by `--role`)|
| `relations USR` | Relations of a symbol (`--direction in\|out`, `--kind`)|
| `neighbors USR` | 1-hop union of relations both directions               |
| `reach USR`     | Transitive closure (`--up\|--down`, `--depth`, `--to-module`) |
| `impact TARGET` | Bidirectional call/usage stacks for a method or type   |

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

| Scenario | Small project | Large project (~340k symbols) |
|---|---|---|
| First-ever query (cold bootstrap) | < 1s | **~15–20s** |
| Warm cache hit (no IndexStore changes) | < 50ms | < 500ms |
| After Xcode incremental rebuild of N files | < 1s | ~3–10s |
| After adding a brand-new source file | full re-bootstrap | full re-bootstrap |
| `reach` warm | < 200ms | < 200ms |

In v3 (Path E) the Swift helper writes the SQLite cache **directly** through
`libsqlite3` — no NDJSON, no Python ingest, no IPC. The helper walks the
IndexStore via the lower-level `IndexStore` Swift wrapper (Apple's
`indexstore-db` package), opening unit and record readers in sequence rather
than issuing per-symbol queries. The combined effect on a real iOS workspace
(WWMobileProject, ~342k symbols, 1.5M relations) is a cold dump in ~15–20s
versus ~3 minutes in v2.

The cache lives at `~/.cache/xcindex/<project-fingerprint>/index.sqlite` and is
**mutable, atomically updated in place**. xcindex tracks each unit's `(size,
mtime)` in the cache; on every query it reads the current `Index.noindex/v5/units/`
listing in milliseconds and computes a delta:

- **No changes** → cache hit, query directly.
- **Modified units only** → invoke the helper's `incremental` subcommand, which
  opens the SQLite, deletes rows for affected files, re-walks the changed
  records, INSERTs the new rows, all in one transaction.
- **Removed units** → helper drops their rows.
- **Added units (new source files)** → fall back to a full re-bootstrap.

Pre-v3 caches (`schema_version != 3`) are detected and rebuilt on first
invocation. v1/v2 `<hash>.sqlite` snapshots from earlier versions are
preserved as `legacy_<hash>.sqlite` for forensics; up to three are kept,
then GC'd.

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
  └─ SQLite cache (~/.cache/xcindex/<project>/index.sqlite, written by Swift helper directly)
```

Source of truth is always the IndexStore. The SQLite cache is materialized
on-the-fly and invalidated automatically when the IndexStore changes.

## Development

```bash
git clone https://github.com/quixabeira-rafael/xcindex
cd xcindex
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/unit -v             # fast, ~3s, ~95 unit tests
.venv/bin/pytest tests -v -m integration   # full suite (~40s), 156 tests, requires Xcode + Swift
```

Layout:
- `src/xcindex/` — Python package
- `swift-helper/` — SwiftPM package; produces `xcindex-helper` binary
- `tests/fixtures/SampleApp/` — SwiftPM project that exercises the full IndexStore
  data model: 14 symbol kinds (class, struct, protocol, enum + cases, typealias,
  destructor, generic / static / instance methods, etc.), 8 sub_kinds (subscript,
  prefix/infix operators, generic-type-param, associated-type, didSet, getter/setter),
  7 relation kinds (childOf, containedBy, calledBy, receivedBy, overrideOf, baseOf,
  extendedBy), and 7 occurrence roles. `tests/integration/test_primitive_coverage.py`
  asserts each primitive survives end-to-end through the helper, the cache, and the CLI.

## Exit codes

- `0` — success
- `1` — usage error (bad args)
- `2` — invalid state (no project found, etc.)
- `3` — system error (IO/permission)
- `4` — stale index (`--require-fresh` and source files newer than index)

## License

MIT.
