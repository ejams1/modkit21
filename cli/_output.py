"""Shared output formatting for the modkit CLI."""

import json
import os
import sys

import click
from pathlib import Path

from cli._query import query_options_active, query_result

JSON_FORMATS = {"json", "compact", "pretty", "jsonl"}


def format_json(data, fmt: str = "json") -> str:
    """Serialize CLI JSON output."""
    if fmt == "pretty":
        return json.dumps(data, indent=2, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def format_json_text(text: str, fmt: str = "json") -> str:
    """Normalize JSON text according to the CLI output format."""
    return format_json(json.loads(text), "pretty" if fmt == "pretty" else "json")


def output(data, fmt: str = "json", *, collection=None, queried=False):
    """Format and print result data."""
    if isinstance(data, dict) and "error" in data:
        emit_error(data["error"], fmt, code=data.get("code", "COMMAND_FAILED"))
        sys.exit(1)

    ctx = click.get_current_context(silent=True)
    options = (ctx.obj or {}).get("report_options", {}) if ctx else {}
    if collection is None and isinstance(data, dict):
        candidates = [key for key in ("records", "results", "matches", "children", "files", "bindings", "changes", "assets", "commands")
                      if isinstance(data.get(key), list)]
        if len(candidates) == 1:
            collection = candidates[0]
    if not queried and query_options_active(options):
        data = query_result(data, options, collection)
    destination = options.get("output")
    if destination:
        path = Path(destination).expanduser().resolve()
        source_ctx = ctx
        while source_ctx:
            for param in source_ctx.command.params:
                if isinstance(param.type, click.Path) and param.type.exists:
                    value = source_ctx.params.get(param.name)
                    values = value if isinstance(value, (tuple, list)) else [value]
                    if any(value and Path(value).resolve() == path for value in values):
                        raise click.BadParameter("Report output must differ from input paths", param_hint="--output")
            source_ctx = source_ctx.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            _write_result(data, fmt, stream, collection)
        print(format_json({"artifact": {"path": str(path), "bytes": path.stat().st_size, "format": fmt},
                           **({"meta": data["meta"]} if isinstance(data, dict) and "meta" in data else {})}))
        return
    _write_result(data, fmt, sys.stdout, collection)


def emit_error(message, fmt="json", *, code="COMMAND_FAILED", exit_code=1):
    if fmt in JSON_FORMATS:
        print(format_json({"error": {"code": code, "message": str(message), "exit_code": exit_code}}), file=sys.stderr)
    else:
        print(f"Error: {message}", file=sys.stderr)


def _write_result(data, fmt, stream, collection=None):
    if fmt == "jsonl":
        if isinstance(data, dict) and "data" in data and "meta" in data:
            rows, meta = data["data"], data["meta"]
        elif collection and isinstance(data, dict):
            rows = data[collection]
            meta = {key: value for key, value in data.items() if key != collection}
        else:
            rows, meta = data if isinstance(data, list) else [data], {}
        for row in rows:
            print(format_json({"kind": "row", "data": row}), file=stream)
        print(format_json({"kind": "meta", "meta": meta}), file=stream)
        return
    if fmt in JSON_FORMATS:
        print(format_json(data, fmt), file=stream)
    elif fmt == "table":
        from contextlib import redirect_stdout
        with redirect_stdout(stream):
            _format_table(data)
    else:
        print(format_json(data), file=stream)


def _format_table(data):
    """Pretty-print data as a table or key-value pairs."""
    if isinstance(data, dict):
        _print_dict(data)
    elif isinstance(data, list):
        if not data:
            print("No results.")
            return
        if isinstance(data[0], dict):
            _print_table(data)
        else:
            for item in data:
                print(item)
    else:
        print(data)


def _print_dict(d: dict):
    if not d:
        return
    max_key = max(len(str(k)) for k in d.keys())
    for k, v in d.items():
        val = str(v)
        if len(val) > 200:
            val = val[:200] + "..."
        print(f"  {k:<{max_key}}  {val}")


def _print_table(rows: list[dict]):
    if not rows:
        return

    skip = {"yaml_content", "source_code", "content", "yaml_path", "script_path"}
    all_keys = []
    for row in rows:
        for k in row:
            if k not in all_keys and k not in skip:
                all_keys.append(k)

    widths = {}
    for k in all_keys:
        w = len(k)
        for row in rows[:50]:
            val = str(row.get(k, ""))
            if len(val) > 60:
                val = val[:60]
            w = max(w, len(val))
        widths[k] = min(w, 60)

    total_width = sum(widths.values()) + (len(all_keys) - 1) * 3
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 200

    if total_width > term_width and len(all_keys) > 3:
        for i, row in enumerate(rows):
            if i > 0:
                print()
            print(f"--- [{i + 1}/{len(rows)}] ---")
            _print_dict({k: row.get(k, "") for k in all_keys})
        return

    header = " | ".join(f"{k:<{widths[k]}}" for k in all_keys)
    print(header)
    print("-+-".join("-" * widths[k] for k in all_keys))

    for row in rows:
        parts = []
        for k in all_keys:
            val = str(row.get(k, ""))
            if len(val) > widths[k]:
                val = val[:widths[k] - 3] + "..."
            parts.append(f"{val:<{widths[k]}}")
        print(" | ".join(parts))

    print(f"\n({len(rows)} results)")
