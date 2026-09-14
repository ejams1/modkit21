from __future__ import annotations

import fnmatch
import json
import operator
import re
from collections import Counter

import click


def values_at(value, path: str) -> list:
    parts = path.replace("[]", ".*").split(".") if path else []

    def walk(node, rest):
        if not rest:
            return [node]
        key, *tail = rest
        if isinstance(node, list):
            if key.isdecimal():
                return walk(node[int(key)], tail) if int(key) < len(node) else []
            return [v for item in node for v in walk(item, tail if key == "*" else rest)]
        if isinstance(node, dict):
            if key == "*":
                return [v for item in node.values() for v in walk(item, tail)]
            return walk(node[key], tail) if key in node else []
        return []

    return walk(value, parts)


def parse_predicate(text):
    match = re.fullmatch(r"(.+?)(!=|>=|<=|=|>|<|~)(.*)", text)
    if not match:
        raise click.BadParameter(f"Expected FIELD=VALUE (also !=, >=, <=, >, <, ~ glob): {text}", param_hint="--where")
    path, op, raw = match.groups()
    try:
        expected = json.loads(raw)
    except json.JSONDecodeError:
        expected = raw

    def predicate(row):
        values = values_at(row, path.strip())
        if not values:
            return False
        comparisons = {"=": operator.eq, "!=": operator.ne, ">": operator.gt,
                       "<": operator.lt, ">=": operator.ge, "<=": operator.le}
        def matches(value):
            if op == "~":
                return isinstance(value, str) and fnmatch.fnmatchcase(value.casefold(), str(expected).casefold())
            try:
                return comparisons[op](value, expected)
            except TypeError:
                return False
        return all(matches(v) for v in values) if op == "!=" else any(matches(v) for v in values)

    return predicate


def project(row, fields):
    if not fields:
        return row
    result = {}
    for field in fields:
        values = values_at(row, field)
        result[field] = values[0] if len(values) == 1 else values if values else None
    return result


def select_rows(rows, options):
    predicates = [parse_predicate(p) for p in options.get("where", ())]
    fields = [s.strip() for s in (options.get("fields") or "").split(",") if s.strip()]
    group_by = options.get("group_by")
    offset, limit = options.get("offset", 0), options.get("limit")
    selected, groups = [], Counter()
    total = matched = 0
    for row in rows:
        total += 1
        if not all(p(row) for p in predicates):
            continue
        matched += 1
        if group_by:
            values = values_at(row, group_by)
            for key in {json.dumps(v, sort_keys=True, ensure_ascii=False) for v in values} or {"null"}:
                groups[key] += 1
        elif not options.get("count_only") and matched > offset and (limit is None or len(selected) < limit):
            selected.append(project(row, fields))
    available = matched
    if group_by:
        grouped = [{"value": json.loads(key), "count": count} for key, count in sorted(groups.items())]
        available = len(grouped)
        selected = [] if options.get("count_only") else grouped[offset:None if limit is None else offset + limit]
    has_more = not options.get("count_only") and offset + len(selected) < available
    return {"data": selected, "meta": {
        "total": total, "matched": matched, "returned": len(selected),
        "offset": offset, "limit": limit, "truncated": has_more,
        "next_offset": offset + len(selected) if has_more and selected else None,
        **({"groups": available, "group_by": group_by} if group_by else {}),
    }}


def query_result(data, options, collection=None):
    items = options.get("items") or collection
    if items:
        found = values_at(data, items)
        if len(found) != 1 or not isinstance(found[0], (list, tuple)):
            raise click.BadParameter(f"{items!r} must identify one result array", param_hint="--items")
        rows = found[0]
    elif isinstance(data, list):
        rows = data
    else:
        rows = [data]
    result = select_rows(rows, options)
    if items:
        result["meta"]["items"] = items
    return result


def query_options_active(options):
    return any(options.get(k) for k in ("fields", "where", "offset", "count_only", "group_by", "items")) or options.get("limit") is not None
