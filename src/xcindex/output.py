from __future__ import annotations

import json
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
        parts.append(_agent_items_block(items))

    if truncated:
        parts.append("_truncated: results were limited; refine query or raise --limit_")

    if warnings:
        parts.append("warnings:")
        parts.extend(f"  - {w}" for w in warnings)

    return "\n\n".join(p for p in parts if p)


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
