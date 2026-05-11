from __future__ import annotations

import json
import shlex
import sys
from typing import Any, IO, Iterable

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID_STATE = 2
EXIT_SYSTEM = 3
EXIT_STALE_INDEX = 4

LEVELS = ("count", "summary", "locations", "detailed")
FORMATS = ("agent", "json", "jsonl", "compact")

DEFAULT_LEVEL = "summary"
DEFAULT_FORMAT = "agent"


def emit_json(payload: Any, stream: IO[str] | None = None) -> None:
    target = stream if stream is not None else sys.stdout
    json.dump(payload, target, ensure_ascii=False, default=_default, indent=None)
    target.write("\n")
    target.flush()


def emit_text(text: str, stream: IO[str] | None = None) -> None:
    target = stream if stream is not None else sys.stdout
    target.write(text)
    if not text.endswith("\n"):
        target.write("\n")
    target.flush()


def emit_error(error: str, message: str, *, json_mode: bool, exit_code: int) -> int:
    if json_mode:
        emit_json({"error": error, "message": message}, stream=sys.stderr)
    else:
        emit_text(f"error: {message}", stream=sys.stderr)
    return exit_code


def render_table(
    rows: Iterable[dict[str, Any]],
    columns: list[tuple[str, str]],
) -> str:
    rows_list = list(rows)
    if not rows_list:
        return ""
    widths = {key: len(label) for key, label in columns}
    for row in rows_list:
        for key, _ in columns:
            value = _stringify(row.get(key))
            widths[key] = max(widths[key], len(value))
    header = "  ".join(label.ljust(widths[key]) for key, label in columns)
    separator = "  ".join("-" * widths[key] for key, _ in columns)
    body_lines = []
    for row in rows_list:
        body_lines.append(
            "  ".join(_stringify(row.get(key)).ljust(widths[key]) for key, _ in columns)
        )
    return "\n".join([header, separator, *body_lines])


def project(canonical: dict[str, Any], level: str) -> dict[str, Any]:
    """Reduce a canonical result dict to the requested level of detail.

    Canonical shape:
        kind:     str        (e.g. "occurrences", "reach", "symbol", "at")
        anchor:   dict       what was queried
        summary:  dict       aggregates (counts, breakdowns)
        items:    list[dict] per-item rows; each item has fields by level
        warnings: list[str]
        truncated:bool
        raw:      dict       optional, present only when --include-raw

    Levels (each is a strict superset of the previous):
        count      -> kind, anchor, summary (only counts/bools), warnings, truncated
        summary    -> + summary breakdowns
        locations  -> + items[].(name, file, line, container)
        detailed   -> + items[].(kind, module, roles, properties, depth) + raw
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level: {level!r}")

    out: dict[str, Any] = {
        "kind": canonical.get("kind"),
        "anchor": canonical.get("anchor"),
        "warnings": list(canonical.get("warnings", [])),
        "truncated": bool(canonical.get("truncated", False)),
    }

    summary = dict(canonical.get("summary") or {})
    if level == "count":
        out["summary"] = _summary_counts_only(summary)
        return _strip_empty(out)

    out["summary"] = summary
    if canonical.get("kind") == "impact":
        out["mode"] = canonical.get("mode")
        if level in ("locations", "detailed"):
            out["stacks"] = canonical.get("stacks") or {"upstream": [], "downstream": []}
            structure = canonical.get("structure")
            if structure is not None:
                out["structure"] = structure
        return _strip_empty(out)
    if canonical.get("kind") == "git":
        if level in ("locations", "detailed"):
            out["files"] = list(canonical.get("files") or [])
        return _strip_empty(out)
    if canonical.get("kind") == "prewarm":
        # All useful info lives in `summary` already; no items/stacks/files.
        return _strip_empty(out)
    if level == "summary":
        return _strip_empty(out)

    items = list(canonical.get("items") or [])
    if level == "locations":
        out["items"] = [_project_location_item(it) for it in items]
        return _strip_empty(out)

    out["items"] = [_project_detailed_item(it) for it in items]
    if "raw" in canonical:
        out["raw"] = canonical["raw"]
    return _strip_empty(out)


def render(projected: dict[str, Any], fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt!r}")
    if fmt == "json":
        return json.dumps(projected, ensure_ascii=False, default=_default)
    if fmt == "jsonl":
        return _render_jsonl(projected)
    if fmt == "compact":
        return _render_compact(projected)
    return _render_agent(projected)


def emit_result(
    canonical: dict[str, Any],
    *,
    level: str = DEFAULT_LEVEL,
    fmt: str = DEFAULT_FORMAT,
    stream: IO[str] | None = None,
) -> None:
    projected = project(canonical, level)
    text = render(projected, fmt)
    emit_text(text, stream=stream)


# --- Internal: projector helpers -----------------------------------------------------

_LOCATION_FIELDS = (
    "name", "kind", "module", "file", "line", "column",
    "container", "container_kind", "depth", "roles", "rel_kind",
)
_DETAILED_EXTRA = (
    "usr", "sub_kind", "language", "properties", "path",
    "rel_roles", "site", "is_system",
)


def _summary_counts_only(summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            out[key] = value
    return out


def _project_location_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _LOCATION_FIELDS if key in item}


def _project_detailed_item(item: dict[str, Any]) -> dict[str, Any]:
    out = _project_location_item(item)
    for key in _DETAILED_EXTRA:
        if key in item:
            out[key] = item[key]
    return out


def _strip_empty(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        out[key] = value
    return out


# --- Internal: renderers --------------------------------------------------------------

def _render_jsonl(projected: dict[str, Any]) -> str:
    head = {k: v for k, v in projected.items() if k != "items"}
    lines = [json.dumps(head, ensure_ascii=False, default=_default)]
    for item in projected.get("items", []):
        lines.append(json.dumps(item, ensure_ascii=False, default=_default))
    return "\n".join(lines)


def _render_compact(projected: dict[str, Any]) -> str:
    items = projected.get("items")
    if not items:
        summary = projected.get("summary") or {}
        if not summary:
            return ""
        return "\t".join(f"{k}={_stringify(v)}" for k, v in summary.items())
    columns = ["file", "line", "name", "kind", "container"]
    rows = []
    for item in items:
        rows.append("\t".join(_stringify(item.get(c)) for c in columns))
    return "\n".join(rows)


def _render_agent(projected: dict[str, Any]) -> str:
    if projected.get("kind") == "impact":
        return _render_impact_agent(projected)
    if projected.get("kind") == "git":
        return _render_git_agent(projected)
    if projected.get("kind") == "prewarm":
        return _render_prewarm_agent(projected)

    parts: list[str] = []
    kind = projected.get("kind")
    anchor = projected.get("anchor") or {}
    summary = projected.get("summary") or {}
    items = projected.get("items") or []
    warnings = projected.get("warnings") or []
    truncated = projected.get("truncated", False)

    headline = _agent_headline(kind, anchor, summary)
    if headline:
        parts.append(headline)

    if summary:
        body = _agent_summary_block(summary)
        if body:
            parts.append(body)

    if items:
        if kind == "file":
            parts.append(render_table(items, [("kind", "kind"), ("name", "name"), ("usr", "usr")]))
        else:
            parts.append(_agent_items_block(items))

    if kind == "file" and items:
        hints = _agent_file_hints(anchor, items)
        if hints:
            parts.append(hints)

    if truncated:
        parts.append("_truncated: results were limited; refine query or raise --limit_")

    if warnings:
        parts.append("warnings:")
        parts.extend(f"  - {w}" for w in warnings)

    return "\n\n".join(p for p in parts if p)


_HINT_TYPE_KINDS = {"class", "struct", "enum", "protocol"}
_HINT_INHERITABLE_KINDS = {"class", "protocol"}
_HINT_EXTENDABLE_KINDS = {"class", "struct", "enum", "protocol"}
_HINT_CALLABLE_KINDS = {
    "instance-method", "class-method", "static-method",
    "function", "constructor", "destructor", "conversion-function",
}
_HINT_OVERRIDABLE_KINDS = _HINT_CALLABLE_KINDS | {
    "instance-property", "class-property", "static-property",
}
_HINT_PROPERTY_KINDS = {
    "instance-property", "class-property", "static-property",
    "field", "variable",
}


def _agent_file_hints(anchor: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Suggest copy-paste commands matched to the kinds present in the file.

    Each command appears at most once, targeted at the most representative item
    for its kind constraint (preferring an item whose name matches the file
    stem). If a kind is absent, its kind-specific commands are skipped.
    """
    file = (anchor or {}).get("file") or ""
    stem = ""
    if file:
        base = file.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base

    def pick(kinds: set[str]) -> dict[str, Any] | None:
        candidates = [it for it in items if it.get("kind") in kinds and it.get("usr")]
        if not candidates:
            return None
        if stem:
            stem_match = next((it for it in candidates if it.get("name") == stem), None)
            if stem_match is not None:
                return stem_match
        return candidates[0]

    primary = pick(_HINT_TYPE_KINDS) or pick(_HINT_CALLABLE_KINDS) or pick(_HINT_PROPERTY_KINDS)
    if primary is None:
        primary = next((it for it in items if it.get("usr")), None)
    if primary is None:
        return ""

    inheritable = pick(_HINT_INHERITABLE_KINDS)
    extendable = pick(_HINT_EXTENDABLE_KINDS)
    type_target = pick(_HINT_TYPE_KINDS)
    callable_target = pick(_HINT_CALLABLE_KINDS)
    overridable_target = pick(_HINT_OVERRIDABLE_KINDS)
    property_target = pick(_HINT_PROPERTY_KINDS)

    primary_name = primary.get("name") or "(unnamed)"
    primary_usr = shlex.quote(primary["usr"])
    suggestions: list[tuple[str, str]] = [
        (f"inspect {primary_name}", f"xcindex symbol {primary_usr}"),
        ("references",               f"xcindex occurrences {primary_usr}"),
        ("blast radius",             f"xcindex reach {primary_usr} --up"),
    ]

    if inheritable is not None:
        usr = shlex.quote(inheritable["usr"])
        suffix = "" if inheritable is primary else f" of {inheritable.get('name')}"
        suggestions.append((f"subclasses/conformers{suffix}",
                            f"xcindex relations {usr} --kind baseOf --direction in"))
    if extendable is not None:
        usr = shlex.quote(extendable["usr"])
        suffix = "" if extendable is primary else f" of {extendable.get('name')}"
        suggestions.append((f"extensions{suffix}",
                            f"xcindex relations {usr} --kind extendedBy --direction in"))
    if type_target is not None:
        usr = shlex.quote(type_target["usr"])
        suffix = "" if type_target is primary else f" of {type_target.get('name')}"
        suggestions.append((f"members{suffix}",
                            f"xcindex relations {usr} --kind containedBy --direction in"))
    if callable_target is not None:
        usr = shlex.quote(callable_target["usr"])
        suffix = "" if callable_target is primary else f" of {callable_target.get('name')}"
        suggestions.append((f"callers{suffix}",
                            f"xcindex occurrences {usr} --role call"))
    if overridable_target is not None:
        usr = shlex.quote(overridable_target["usr"])
        suffix = "" if overridable_target is primary else f" of {overridable_target.get('name')}"
        suggestions.append((f"override chain{suffix}",
                            f"xcindex relations {usr} --kind overrideOf --direction in"))
    if property_target is not None:
        usr = shlex.quote(property_target["usr"])
        prop_suffix = "" if property_target is primary else f" of {property_target.get('name')}"
        suggestions.append((f"reads{prop_suffix}",
                            f"xcindex occurrences {usr} --role read"))
        suggestions.append((f"writes{prop_suffix}",
                            f"xcindex occurrences {usr} --role write"))

    label_width = max(len(label) for label, _ in suggestions)
    lines = ["**next steps**"]
    for label, command in suggestions:
        lines.append(f"  {(label + ':').ljust(label_width + 2)} {command}")
    return "\n".join(lines)


def _agent_headline(kind: Any, anchor: dict[str, Any], summary: dict[str, Any]) -> str:
    found = summary.get("found")
    count = summary.get("count")
    head = f"## {kind}" if kind else "## result"
    anchor_label = anchor.get("name") or anchor.get("usr") or anchor.get("file")
    if anchor_label:
        head += f" — {anchor_label}"
    bits: list[str] = []
    if isinstance(found, bool):
        bits.append("found" if found else "not found")
    if isinstance(count, int):
        bits.append(f"{count} item{'s' if count != 1 else ''}")
    if bits:
        head += f" ({', '.join(bits)})"
    return head


def _agent_summary_block(summary: dict[str, Any]) -> str:
    interesting = {
        k: v for k, v in summary.items()
        if k not in ("found", "count") and v not in (None, [], {})
    }
    if not interesting:
        return ""
    lines = ["**summary**"]
    for key, value in interesting.items():
        lines.append(f"  - {key}: {_format_summary_value(value)}")
    return "\n".join(lines)


def _agent_items_block(items: list[dict[str, Any]]) -> str:
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        file = item.get("file") or "(unknown)"
        by_file.setdefault(file, []).append(item)
    blocks = ["**locations**"]
    for file, group in by_file.items():
        blocks.append(f"\n{file} ({len(group)})")
        for item in group:
            blocks.append("  " + _format_item_line(item))
    return "\n".join(blocks)


def _format_item_line(item: dict[str, Any]) -> str:
    name = item.get("name") or item.get("usr") or "(unnamed)"
    line = item.get("line")
    column = item.get("column")
    container = item.get("container")
    kind = item.get("kind")
    depth = item.get("depth")

    parts = [name]
    if line is not None:
        loc = f"L{line}"
        if column is not None:
            loc += f":{column}"
        parts.append(loc)
    if kind:
        parts.append(f"[{kind}]")
    if container:
        parts.append(f"in {container}")
    if depth is not None:
        parts.append(f"({depth} hops)")
    return " ".join(parts)


def _format_summary_value(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{k}={v}" for k, v in value.items())
        return f"{{{inner}}}"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


# --- Internal: prewarm renderer --------------------------------------------------


def _render_prewarm_agent(projected: dict[str, Any]) -> str:
    summary = projected.get("summary") or {}
    anchor = projected.get("anchor") or {}
    mode = summary.get("mode", "noop")
    seconds = summary.get("wall_seconds", 0.0)
    parts = [f"## prewarm — {mode} ({seconds:.1f}s)"]
    project = anchor.get("project")
    if project:
        parts.append(f"  project: {project}")
    if mode in ("cold", "schema_upgrade"):
        parts.append(
            f"  bootstrapped: {summary.get('symbols_added', 0)} symbols, "
            f"{summary.get('occurrences_added', 0)} occurrences, "
            f"{summary.get('relations_added', 0)} relations"
        )
        if summary.get("units_added"):
            parts.append(f"  trigger: {summary['units_added']} new unit(s)")
    elif mode == "incremental":
        parts.append(
            f"  modified: {summary.get('units_modified', 0)} unit(s), "
            f"removed: {summary.get('units_removed', 0)} unit(s)"
        )
        parts.append(
            f"  delta: +{summary.get('symbols_added', 0)} symbols, "
            f"+{summary.get('occurrences_added', 0)} occurrences, "
            f"+{summary.get('relations_added', 0)} relations"
        )
    return "\n".join(parts)


# --- Internal: git renderer ------------------------------------------------------


def _render_git_agent(projected: dict[str, Any]) -> str:
    parts: list[str] = []
    anchor = projected.get("anchor") or {}
    summary = projected.get("summary") or {}
    files = projected.get("files") or []
    warnings = projected.get("warnings") or []

    label = anchor.get("label") or "git changes"
    file_count = summary.get("files", 0)
    sym_count = summary.get("modified_symbols", 0)
    parts.append(
        f"## git changes — {label} ({file_count} file{'s' if file_count != 1 else ''}, "
        f"{sym_count} modified symbol{'s' if sym_count != 1 else ''})"
    )

    by_status = summary.get("by_status") or {}
    if by_status:
        parts.append("**summary**\n" + "  - by_status: " + _format_summary_value(by_status))

    if not files:
        parts.append("_no indexable file changes detected._")
    else:
        for entry in files:
            parts.append(_git_file_block(entry))

        suggestion_block = _git_suggestions_block(files)
        if suggestion_block:
            parts.append(suggestion_block)

    if warnings:
        parts.append("warnings:")
        parts.extend(f"  - {w}" for w in warnings)

    return "\n\n".join(p for p in parts if p)


def _git_file_block(entry: dict[str, Any]) -> str:
    lines: list[str] = []
    path = entry.get("path") or "(unknown)"
    status = entry.get("status") or "?"
    old_path = entry.get("old_path")
    note = entry.get("note")
    symbols = entry.get("symbols") or []

    header = f"{path}  [{status}]"
    if old_path:
        header += f"  (renamed from {old_path})"
    lines.append(header)

    if note:
        lines.append(f"  ⚠ {note}")
    if not symbols:
        if not note:
            lines.append("  (no enclosing symbols resolved at modified ranges)")
        return "\n".join(lines)

    name_w = min(48, max((len(s.get("name") or "") for s in symbols), default=8))
    kind_w = min(20, max((len(s.get("kind") or "") for s in symbols), default=8))
    for sym in symbols:
        rng = sym.get("modified_range") or [None, None]
        loc = f"L{rng[0]}-{rng[1]}" if rng[0] != rng[1] else f"L{rng[0]}"
        name = (sym.get("name") or "(unnamed)").ljust(name_w)
        kind = (sym.get("kind") or "?").ljust(kind_w)
        usr = sym.get("usr") or "(no usr)"
        lines.append(f"  {loc:<10s}  {name}  [{kind}]  {usr}")
    return "\n".join(lines)


def _git_suggestions_block(files: list[dict[str, Any]]) -> str:
    file_cmds: list[str] = []
    impact_cmds: list[str] = []
    seen_paths: set[str] = set()
    seen_usrs: set[str] = set()
    for entry in files:
        path = entry.get("path") or ""
        if entry.get("status") in ("modified", "renamed", "copied", "added") and path and path not in seen_paths:
            seen_paths.add(path)
            file_cmds.append(f"xcindex file {shlex.quote(path)}")
        for sym in entry.get("symbols") or []:
            usr = sym.get("usr")
            if not usr or usr in seen_usrs:
                continue
            seen_usrs.add(usr)
            impact_cmds.append(f"xcindex impact {shlex.quote(usr)}")

    if not (file_cmds or impact_cmds):
        return ""
    parts = ["**next steps**"]
    if file_cmds:
        parts.append("  list types in modified files:")
        for cmd in file_cmds:
            parts.append(f"    {cmd}")
    if impact_cmds:
        parts.append("  blast radius of each modified symbol:")
        for cmd in impact_cmds:
            parts.append(f"    {cmd}")
    return "\n".join(parts)


# --- Internal: impact renderer ---------------------------------------------------

_IMPACT_HINT_KIND_MAP = {
    "instance-property": ("read sites", "write sites", "containing type"),
    "class-property":    ("read sites", "write sites", "containing type"),
    "static-property":   ("read sites", "write sites", "containing type"),
    "field":             ("read sites", "write sites"),
    "variable":          ("read sites", "write sites"),
    "extension":         ("members", "extended type"),
    "typealias":         ("references",),
    "enum-case":         ("references", "containing enum"),
    "parameter":         ("references",),
}


def _render_impact_agent(projected: dict[str, Any]) -> str:
    parts: list[str] = []
    mode = projected.get("mode") or "hint_only"
    anchor = projected.get("anchor") or {}
    summary = projected.get("summary") or {}
    stacks = projected.get("stacks") or {"upstream": [], "downstream": []}
    structure = projected.get("structure") or None
    warnings = projected.get("warnings") or []
    truncated = projected.get("truncated", False)

    parts.append(_impact_headline(mode, anchor, summary))

    summary_block = _impact_summary_block(mode, summary)
    if summary_block:
        parts.append(summary_block)

    if mode in ("call_stack", "usage_chain"):
        upstream = stacks.get("upstream") or []
        downstream = stacks.get("downstream") or []
        if upstream:
            parts.append(_impact_stacks_block("upstream", upstream))
        if downstream:
            parts.append(_impact_stacks_block("downstream", downstream))
        if not upstream and not downstream and mode == "call_stack":
            parts.append("_no transitive callers/callees found in indexed code._")

    if structure:
        block = _impact_structure_block(structure)
        if block:
            parts.append(block)

    hints = _impact_hints(mode, anchor)
    if hints:
        parts.append(hints)

    if truncated:
        parts.append("_truncated: stacks were limited; raise --max-stacks or --depth_")

    if warnings:
        parts.append("warnings:")
        parts.extend(f"  - {w}" for w in warnings)

    return "\n\n".join(p for p in parts if p)


def _impact_headline(mode: str, anchor: dict[str, Any], summary: dict[str, Any]) -> str:
    name = anchor.get("name") or anchor.get("usr") or "(target)"
    kind = anchor.get("kind")
    module = anchor.get("module")
    suffix_bits: list[str] = []
    if kind:
        suffix_bits.append(f"{kind}")
    if module:
        suffix_bits.append(module)
    suffix = f" [{' / '.join(suffix_bits)}]" if suffix_bits else ""

    if mode == "call_stack":
        up = summary.get("upstream", {}) or {}
        down = summary.get("downstream", {}) or {}
        return (
            f"## impact — {name}{suffix} "
            f"({up.get('stacks', 0)} upstream, {down.get('stacks', 0)} downstream)"
        )
    if mode == "usage_chain":
        up = summary.get("upstream", {}) or {}
        sc = summary.get("structure_counts", {}) or {}
        return (
            f"## impact — {name}{suffix} "
            f"(type — {up.get('transitive_count', 0)} transitive users, "
            f"{sc.get('members', 0)} members)"
        )
    return f"## impact — {name}{suffix}"


def _impact_summary_block(mode: str, summary: dict[str, Any]) -> str:
    if mode == "hint_only":
        reason = summary.get("reason")
        return f"_{reason}_" if reason else ""

    lines = ["**summary**"]
    up = summary.get("upstream") or {}
    if up:
        lines.append(
            f"  - upstream:   {up.get('transitive_count', 0)} transitive callers, "
            f"{up.get('module_count', 0)} modules"
        )
        if up.get("by_module"):
            lines.append(f"      by_module: {_format_summary_value(up['by_module'])}")
        if up.get("by_depth"):
            lines.append(f"      by_depth:  {_format_summary_value(up['by_depth'])}")
        if up.get("by_edge_kind") and set(up["by_edge_kind"]) - {"calledBy"}:
            lines.append(f"      by_edge:   {_format_summary_value(up['by_edge_kind'])}")

    down = summary.get("downstream") or {}
    if down and down.get("stacks"):
        lines.append(
            f"  - downstream: {down.get('transitive_count', 0)} transitive callees, "
            f"{down.get('module_count', 0)} modules"
        )
        if down.get("by_depth"):
            lines.append(f"      by_depth:  {_format_summary_value(down['by_depth'])}")

    sc = summary.get("structure_counts") or {}
    if sc:
        lines.append(
            f"  - structure:  {sc.get('members', 0)} members, "
            f"{sc.get('subclasses', 0)} subclasses, "
            f"{sc.get('extensions', 0)} extensions"
        )

    return "\n".join(lines) if len(lines) > 1 else ""


def _impact_stacks_block(label: str, stacks: list[list[dict[str, Any]]]) -> str:
    blocks: list[str] = []
    for index, stack in enumerate(stacks, start=1):
        depth = max(0, len(stack) - 1)
        non_default_edges = sorted({
            f["edge_kind"] for f in stack
            if f.get("edge_kind") and f["edge_kind"] != "calledBy"
        })
        edge_suffix = f" ({', '.join(non_default_edges)})" if non_default_edges else ""
        blocks.append(f"[{label} stack {index}] depth {depth}{edge_suffix}")
        for n, frame in enumerate(stack):
            blocks.append("  " + _format_impact_frame(n, frame))
    return "\n".join(blocks)


def _format_impact_frame(index: int, frame: dict[str, Any]) -> str:
    name = frame.get("name") or "(unnamed)"
    file = frame.get("file")
    line = frame.get("line")
    module = frame.get("module")
    is_target = bool(frame.get("is_target"))

    if file and line is not None:
        loc = f"{file}:{line}"
    elif file:
        loc = file
    else:
        loc = "(external)"
    if module:
        loc = f"{module}/{loc.split('/')[-1] if '/' in loc else loc}" if file else f"{module} {loc}"

    target_marker = "  ← target" if is_target else ""
    return f"#{index}  {name:<48s} {loc}{target_marker}"


def _impact_structure_block(structure: dict[str, list[dict[str, Any]]]) -> str:
    members = structure.get("members") or []
    subclasses = structure.get("subclasses") or []
    extensions = structure.get("extensions") or []
    if not (members or subclasses or extensions):
        return ""
    def _safe(value, default="(unnamed)"):
        return value if value is not None else default

    lines = ["**structure**"]
    if members:
        lines.append(f"  members ({len(members)}):")
        for row in members[:50]:
            name = _safe(row.get("name"))
            kind = _safe(row.get("kind"), "?")
            file = _safe(row.get("file"), "(external)")
            line_no = _safe(row.get("line"), "?")
            lines.append(f"    {name:<32s} {kind:<20s} {file}:{line_no}")
        if len(members) > 50:
            lines.append(f"    ... ({len(members) - 50} more)")
    if subclasses:
        lines.append(f"  subclasses ({len(subclasses)}):")
        for row in subclasses[:25]:
            name = _safe(row.get("name"))
            file = _safe(row.get("file"), "(external)")
            line_no = _safe(row.get("line"), "?")
            lines.append(f"    {name:<32s} {file}:{line_no}")
        if len(subclasses) > 25:
            lines.append(f"    ... ({len(subclasses) - 25} more)")
    if extensions:
        lines.append(f"  extensions ({len(extensions)}):")
        for row in extensions[:25]:
            name = _safe(row.get("name"))
            file = _safe(row.get("file"), "(external)")
            line_no = _safe(row.get("line"), "?")
            lines.append(f"    {name:<32s} {file}:{line_no}")
        if len(extensions) > 25:
            lines.append(f"    ... ({len(extensions) - 25} more)")
    return "\n".join(lines)


def _impact_hints(mode: str, anchor: dict[str, Any]) -> str:
    usr = anchor.get("usr")
    if not usr:
        return ""
    qusr = shlex.quote(usr)

    suggestions: list[tuple[str, str]] = []
    if mode == "call_stack":
        suggestions = [
            ("full graph (raw)",   f"xcindex reach {qusr} --up --limit 5000"),
            ("filter by module",   f"xcindex impact {qusr} --to-module <Module>"),
            ("direct callers",     f"xcindex relations {qusr} --kind calledBy --direction in"),
            ("direct callees",     f"xcindex relations {qusr} --kind calledBy --direction out"),
        ]
    elif mode == "usage_chain":
        suggestions = [
            ("drill into a member", f"xcindex impact '<MemberName>'"),
            ("list all references", f"xcindex occurrences {qusr}"),
            ("silent inheritors",   f"xcindex relations {qusr} --kind baseOf --direction in"),
            ("extensions",          f"xcindex relations {qusr} --kind extendedBy --direction in"),
        ]
    else:
        kind = anchor.get("kind") or ""
        flavors = _IMPACT_HINT_KIND_MAP.get(kind, ("references", "blast radius"))
        for flavor in flavors:
            if flavor == "read sites":
                suggestions.append(("read sites", f"xcindex occurrences {qusr} --role read"))
            elif flavor == "write sites":
                suggestions.append(("write sites", f"xcindex occurrences {qusr} --role write"))
            elif flavor == "containing type":
                suggestions.append(("containing type",
                                    f"xcindex relations {qusr} --kind containedBy --direction out"))
            elif flavor == "members":
                suggestions.append(("members",
                                    f"xcindex relations {qusr} --kind containedBy --direction in"))
            elif flavor == "extended type":
                suggestions.append(("extended type",
                                    f"xcindex relations {qusr} --kind extendedBy --direction out"))
            elif flavor == "containing enum":
                suggestions.append(("containing enum",
                                    f"xcindex relations {qusr} --kind containedBy --direction out"))
            elif flavor == "references":
                suggestions.append(("references", f"xcindex occurrences {qusr}"))
            elif flavor == "blast radius":
                suggestions.append(("blast radius", f"xcindex reach {qusr} --up"))

    if not suggestions:
        return ""
    label_width = max(len(label) for label, _ in suggestions)
    lines = ["**next steps**"]
    for label, command in suggestions:
        lines.append(f"  {(label + ':').ljust(label_width + 2)} {command}")
    return "\n".join(lines)


# --- Internal: shared --------------------------------------------------------------

def _stringify(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")
