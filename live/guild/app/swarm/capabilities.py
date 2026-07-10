"""L2 — Capability Seed Factory: the Pilot A cohort of 16 narrow, deterministic,
side-effect-free capabilities.

Every capability here is a pure function over its JSON payload: no network, no
filesystem, no eval, no model call. That is deliberate — a rational external
agent should invoke these because they are faster, cheaper, and more
deterministic than a model completion, and safe because they cannot touch
anything. New capabilities enter ONLY via this template (schemas + fixtures +
failure modes + prohibited uses); an identity is published only after its
fixture suite passes (see identity.validate_all / the publish gate).
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Callable, Optional

from ..crypto import canonicalize_jcs

# jsonschema and dateutil are the only non-stdlib deps (mature, pinned).
import jsonschema
from dateutil import parser as dateparser


class CapabilityError(ValueError):
    """Raised by a capability for a well-formed but unprocessable payload.
    Converted to a structured, non-5xx error by the gateway."""


# --------------------------------------------------------------------------
# template
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    id: str                       # machine identifier, e.g. "json.repair"
    version: str                  # semver of THIS capability implementation
    name: str                     # optional human-readable metadata
    summary: str                  # one-line machine-oriented summary
    description: str              # natural-language description for LLM clients
    tags: tuple
    input_schema: dict
    output_schema: dict
    run: Callable[[dict], dict]
    fixtures: tuple               # ({"input": ..., "expect_subset": ...}, ...)
    failure_modes: tuple
    prohibited_uses: tuple
    safety_class: str = "deterministic_transform"
    demand_hypothesis: str = ""
    baseline: str = ("generic model completion: slower, non-deterministic, "
                     "costlier, and unverifiable")
    est_cost_credits: int = 0     # guest tier is free within limits
    est_latency_ms: int = 20
    context_limits: dict = field(default_factory=lambda: {"max_payload_bytes": 65536})


def _obj(props: dict, required: list, **extra) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False, **extra}


# --------------------------------------------------------------------------
# implementations
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*\n(.*?)\n\s*```\s*$", re.S)
_UNQUOTED_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)')
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SMART_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments outside of strings (string-aware scan)."""
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _single_to_double_quotes(text: str) -> str:
    """Convert single-quoted strings to double-quoted (string-aware)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':                       # skip existing double-quoted strings
            out.append(c)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\":
                    i += 1
                    if i < n:
                        out.append(text[i])
                elif text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "'":
            buf, i = [], i + 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i:i + 2])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # closing '
            inner = "".join(buf).replace('\\"', '"').replace('"', '\\"').replace("\\'", "'")
            out.append('"' + inner + '"')
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _balance_brackets(text: str) -> str:
    stack = []
    in_str = esc = False
    for c in text:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack and stack[-1] == c:
            stack.pop()
    return text + "".join(reversed(stack))


def _run_json_repair(payload: dict) -> dict:
    text, steps = payload["text"], []

    def attempt(t):
        try:
            return json.loads(t)
        except (json.JSONDecodeError, ValueError):
            return _SENTINEL

    _SENTINEL = object()
    parsed = attempt(text)
    if parsed is not _SENTINEL:
        return {"ok": True, "parsed": parsed, "repaired": text, "changed": False,
                "steps": []}
    m = _FENCE_RE.match(text)
    if m:
        text, _ = m.group(1), steps.append("stripped_code_fence")
    for bad, good in _SMART_QUOTES.items():
        if bad in text:
            text = text.replace(bad, good)
            if "normalized_smart_quotes" not in steps:
                steps.append("normalized_smart_quotes")
    stripped = _strip_comments(text)
    if stripped != text:
        text, _ = stripped, steps.append("removed_comments")
    converted = _single_to_double_quotes(text)
    if converted != text:
        text, _ = converted, steps.append("converted_single_quotes")
    quoted = _UNQUOTED_KEY_RE.sub(r'\1"\2"\3', text)
    if quoted != text:
        text, _ = quoted, steps.append("quoted_bare_keys")
    for lit, repl in (("True", "true"), ("False", "false"), ("None", "null"),
                      ("NaN", "null"), ("Infinity", "null"), ("undefined", "null")):
        replaced = re.sub(r"\b%s\b" % lit, repl, text)
        if replaced != text:
            text, _ = replaced, steps.append(f"replaced_literal_{lit}")
    detrailed = _TRAILING_COMMA_RE.sub(r"\1", text)
    if detrailed != text:
        text, _ = detrailed, steps.append("removed_trailing_commas")
    balanced = _balance_brackets(text.rstrip().rstrip(","))
    if balanced != text:
        text, _ = balanced, steps.append("balanced_brackets")
    parsed = attempt(text)
    if parsed is _SENTINEL:
        return {"ok": False, "parsed": None, "repaired": text, "changed": bool(steps),
                "steps": steps}
    return {"ok": True, "parsed": parsed, "repaired": text, "changed": True,
            "steps": steps}


def _run_json_validate(payload: dict) -> dict:
    schema = payload["schema"]
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise CapabilityError(f"invalid JSON Schema: {e.message}")
    validator = validator_cls(schema)
    errors = []
    for err in validator.iter_errors(payload["instance"]):
        errors.append({"path": "/" + "/".join(str(p) for p in err.absolute_path),
                       "message": err.message,
                       "validator": str(err.validator)})
        if len(errors) >= 50:
            break
    return {"valid": not errors, "errors": errors, "error_count": len(errors)}


def _infer_schema(values: list) -> dict:
    types = set()
    for v in values:
        types.add({bool: "boolean", int: "integer", float: "number", str: "string",
                   type(None): "null"}.get(type(v), "array" if isinstance(v, list)
                                           else "object" if isinstance(v, dict) else "string"))
    if types == {"integer", "number"}:
        types = {"number"}
    if len(types) == 1:
        t = types.pop()
        schema: dict[str, Any] = {"type": t}
        if t == "object":
            all_keys: dict[str, list] = {}
            objs = [v for v in values if isinstance(v, dict)]
            for o in objs:
                for k, v in o.items():
                    all_keys.setdefault(k, []).append(v)
            schema["properties"] = {k: _infer_schema(vs) for k, vs in all_keys.items()}
            required = [k for k, vs in all_keys.items() if len(vs) == len(objs)]
            if required:
                schema["required"] = sorted(required)
        elif t == "array":
            items = [x for v in values if isinstance(v, list) for x in v]
            if items:
                schema["items"] = _infer_schema(items)
        return schema
    return {"type": sorted(types)}


def _run_schema_infer(payload: dict) -> dict:
    examples = payload["examples"]
    if not examples:
        raise CapabilityError("examples must be a non-empty array")
    schema = _infer_schema(examples)
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    return {"schema": schema, "examples_used": len(examples)}


def _run_canonicalize(payload: dict) -> dict:
    canonical = canonicalize_jcs(payload["value"])
    return {"canonical": canonical,
            "sha256": sha256(canonical.encode()).hexdigest(),
            "bytes": len(canonical.encode())}


def _json_diff(a: Any, b: Any, path: str, out: list, cap: int) -> None:
    if len(out) >= cap:
        return
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append({"path": path or "/", "kind": "changed", "a": a, "b": b})
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}/{k}"
            if k not in b:
                out.append({"path": p, "kind": "removed", "a": a[k]})
            elif k not in a:
                out.append({"path": p, "kind": "added", "b": b[k]})
            else:
                _json_diff(a[k], b[k], p, out, cap)
            if len(out) >= cap:
                return
    elif isinstance(a, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}/{i}"
            if i >= len(b):
                out.append({"path": p, "kind": "removed", "a": a[i]})
            elif i >= len(a):
                out.append({"path": p, "kind": "added", "b": b[i]})
            else:
                _json_diff(a[i], b[i], p, out, cap)
            if len(out) >= cap:
                return
    elif a != b:
        out.append({"path": path or "/", "kind": "changed", "a": a, "b": b})


def _run_json_diff(payload: dict) -> dict:
    changes: list = []
    _json_diff(payload["a"], payload["b"], "", changes, 500)
    return {"equal": not changes, "changes": changes, "change_count": len(changes)}


_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _run_path_extract(payload: dict) -> dict:
    results = []
    for path in payload["paths"]:
        node, found = payload["value"], True
        for m in _PATH_TOKEN_RE.finditer(path):
            key, idx = m.group(1), m.group(2)
            try:
                node = node[int(idx)] if idx is not None else node[key]
            except (KeyError, IndexError, TypeError):
                found = False
                break
        results.append({"path": path, "found": found,
                        "value": node if found else None})
    return {"results": results}


def _run_csv_to_json(payload: dict) -> dict:
    text = payload["csv"]
    delimiter = payload.get("delimiter")
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise CapabilityError("no rows found")
    if payload.get("has_header", True):
        columns, data = [c.strip() for c in rows[0]], rows[1:]
    else:
        columns, data = [f"col_{i}" for i in range(len(rows[0]))], rows
    out = [{columns[i] if i < len(columns) else f"col_{i}": cell
            for i, cell in enumerate(r)} for r in data]
    return {"rows": out, "columns": columns, "count": len(out),
            "delimiter": delimiter}


def _run_json_to_csv(payload: dict) -> dict:
    rows = payload["rows"]
    if not rows:
        raise CapabilityError("rows must be non-empty")
    columns = payload.get("columns") or sorted({k for r in rows for k in r})
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([("" if r.get(c) is None else
                     json.dumps(r[c]) if isinstance(r.get(c), (dict, list))
                     else r[c]) for c in columns])
    return {"csv": buf.getvalue(), "columns": columns, "count": len(rows)}


_MD_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _run_markdown_table(payload: dict) -> dict:
    lines = payload["markdown"].splitlines()
    tables, i = [], 0

    def cells(line: str) -> list:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and _MD_SEP_RE.match(lines[i + 1])):
            columns = cells(lines[i])
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and not _MD_SEP_RE.match(lines[i]):
                row = cells(lines[i])
                row += [""] * (len(columns) - len(row))
                rows.append(row[:len(columns)])
                i += 1
            tables.append({"columns": columns, "rows": rows, "row_count": len(rows)})
        else:
            i += 1
    return {"tables": tables, "count": len(tables)}


def _run_date_normalize(payload: dict) -> dict:
    results = []
    dayfirst = bool(payload.get("dayfirst", False))
    for raw in payload["dates"]:
        try:
            dt = dateparser.parse(raw, dayfirst=dayfirst, fuzzy=False)
            results.append({"input": raw, "ok": True, "iso": dt.isoformat(),
                            "date": dt.date().isoformat()})
        except (ValueError, OverflowError, TypeError) as e:
            results.append({"input": raw, "ok": False, "iso": None, "date": None,
                            "error": str(e)[:200]})
    ok = sum(1 for r in results if r["ok"])
    return {"results": results, "parsed": ok, "failed": len(results) - ok}


def _dedupe_key(rec: Any, keys: Optional[list], ci: bool) -> str:
    if keys and isinstance(rec, dict):
        basis: Any = {k: rec.get(k) for k in keys}
    else:
        basis = rec
    s = canonicalize_jcs(basis)
    return s.lower() if ci else s


def _run_dedupe(payload: dict) -> dict:
    keys = payload.get("keys")
    ci = bool(payload.get("case_insensitive", False))
    seen: dict[str, int] = {}
    unique, dup_counts = [], {}
    for rec in payload["records"]:
        k = _dedupe_key(rec, keys, ci)
        if k in seen:
            dup_counts[k] = dup_counts.get(k, 1) + 1
        else:
            seen[k] = len(unique)
            unique.append(rec)
    duplicates = [{"record": unique[seen[k]], "count": c}
                  for k, c in dup_counts.items()]
    return {"unique": unique, "kept": len(unique),
            "removed": len(payload["records"]) - len(unique),
            "duplicates": duplicates}


def _norm_str(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _run_record_link(payload: dict) -> dict:
    import difflib
    left, right = payload["left"], payload["right"]
    if len(left) * len(right) > 250000:
        raise CapabilityError("left×right too large (cap 250000 comparisons)")
    lk, rk = payload["left_key"], payload["right_key"]
    threshold = float(payload.get("threshold", 0.85))
    scored = []
    for i, l in enumerate(left):
        for j, r in enumerate(right):
            a, b = _norm_str(l.get(lk, "")), _norm_str(r.get(rk, ""))
            if not a or not b:
                continue
            score = difflib.SequenceMatcher(None, a, b).ratio()
            if score >= threshold:
                scored.append((score, i, j))
    scored.sort(reverse=True)
    used_l, used_r, matches = set(), set(), []
    for score, i, j in scored:
        if i in used_l or j in used_r:
            continue
        used_l.add(i)
        used_r.add(j)
        matches.append({"left_index": i, "right_index": j,
                        "score": round(score, 4)})
    return {"matches": matches,
            "unmatched_left": [i for i in range(len(left)) if i not in used_l],
            "unmatched_right": [j for j in range(len(right)) if j not in used_r]}


_NESTED_QUANT_RE = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")


def _run_regex_extract(payload: dict) -> dict:
    pattern = payload["pattern"]
    if len(pattern) > 300:
        raise CapabilityError("pattern too long (max 300 chars)")
    if _NESTED_QUANT_RE.search(pattern):
        raise CapabilityError("pattern rejected: nested quantifiers risk "
                              "catastrophic backtracking")
    flags = 0
    for f in payload.get("flags", []):
        flags |= {"i": re.I, "m": re.M, "s": re.S}.get(f, 0)
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        raise CapabilityError(f"invalid pattern: {e}")
    cap = min(int(payload.get("max_matches", 1000)), 1000)
    matches = []
    for m in rx.finditer(payload["text"]):
        matches.append({"match": m.group(0), "groups": list(m.groups()),
                        "start": m.start(), "end": m.end()})
        if len(matches) >= cap:
            break
    return {"matches": matches, "count": len(matches), "truncated": len(matches) >= cap}


_UNITS: dict[str, dict[str, float]] = {
    "length": {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mi": 1609.344,
               "yd": 0.9144, "ft": 0.3048, "in": 0.0254, "nmi": 1852},
    "mass": {"kg": 1, "g": 0.001, "mg": 1e-6, "t": 1000, "lb": 0.45359237,
             "oz": 0.028349523125},
    "time": {"s": 1, "ms": 0.001, "us": 1e-6, "min": 60, "h": 3600, "d": 86400,
             "wk": 604800},
    "data": {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024,
             "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "bit": 0.125},
}


def _run_unit_convert(payload: dict) -> dict:
    value, u_from, u_to = float(payload["value"]), payload["from"].lower(), payload["to"].lower()
    temp = {"c", "f", "k"}
    if u_from in temp and u_to in temp:
        c = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[u_from]
        result = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[u_to]
        return {"value": value, "from": u_from, "to": u_to,
                "result": round(result, 10), "dimension": "temperature"}
    for dim, table in _UNITS.items():
        if u_from in table and u_to in table:
            result = value * table[u_from] / table[u_to]
            return {"value": value, "from": u_from, "to": u_to,
                    "result": round(result, 12), "dimension": dim}
    raise CapabilityError(f"cannot convert {u_from!r} -> {u_to!r}: unknown or "
                          f"incompatible units")


_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$")


def _parse_semver(v: str):
    m = _SEMVER_RE.match(v.strip())
    if not m:
        raise CapabilityError(f"not a semver version: {v!r}")
    pre = m.group(4)
    pre_ids: tuple = ()
    if pre:
        pre_ids = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre_ids)


def _cmp_semver(a, b) -> int:
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    pa, pb = a[3], b[3]
    if pa == pb:
        return 0
    if not pa:
        return 1        # release > prerelease
    if not pb:
        return -1
    return -1 if pa < pb else 1


def _satisfies(version, constraint: str) -> bool:
    for clause in constraint.replace(",", " ").split():
        m = re.match(r"^(>=|<=|>|<|=|\^|~)?(.+)$", clause)
        op, ref_s = m.group(1) or "=", m.group(2)
        ref = _parse_semver(ref_s)
        c = _cmp_semver(version, ref)
        if op == "=" and c != 0:
            return False
        if op == ">" and c <= 0:
            return False
        if op == ">=" and c < 0:
            return False
        if op == "<" and c >= 0:
            return False
        if op == "<=" and c > 0:
            return False
        if op == "^":
            upper = (ref[0] + 1, 0, 0, ()) if ref[0] > 0 else (0, ref[1] + 1, 0, ())
            if c < 0 or _cmp_semver(version, upper) >= 0:
                return False
        if op == "~":
            upper = (ref[0], ref[1] + 1, 0, ())
            if c < 0 or _cmp_semver(version, upper) >= 0:
                return False
    return True


def _run_semver(payload: dict) -> dict:
    if "constraint" in payload and payload.get("constraint"):
        v = _parse_semver(payload["version"])
        return {"version": payload["version"], "constraint": payload["constraint"],
                "satisfies": _satisfies(v, payload["constraint"])}
    if not payload.get("a") or not payload.get("b"):
        raise CapabilityError("provide either {a,b} to compare or {version,constraint}")
    c = _cmp_semver(_parse_semver(payload["a"]), _parse_semver(payload["b"]))
    return {"a": payload["a"], "b": payload["b"], "comparison": c,
            "relation": {-1: "a<b", 0: "a=b", 1: "a>b"}[c]}


def _run_stats(payload: dict) -> dict:
    values = [float(v) for v in payload["values"]]
    if not values:
        raise CapabilityError("values must be non-empty")
    if any(math.isnan(v) or math.isinf(v) for v in values):
        raise CapabilityError("values must be finite numbers")
    sv = sorted(values)

    def pct(p: float) -> float:
        k = (len(sv) - 1) * p / 100
        f, c = math.floor(k), math.ceil(k)
        return sv[int(k)] if f == c else sv[f] + (sv[c] - sv[f]) * (k - f)

    out = {"count": len(values), "sum": sum(values), "min": sv[0], "max": sv[-1],
           "mean": statistics.fmean(values), "median": statistics.median(values),
           "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
           "variance": statistics.variance(values) if len(values) > 1 else 0.0,
           "percentiles": {str(p): pct(float(p))
                           for p in payload.get("percentiles", [25, 50, 75, 95, 99])}}
    return out


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

_ANY = {}  # JSON Schema "any" — no constraint

CAPABILITIES: dict[str, Capability] = {}


def _register(cap: Capability) -> None:
    CAPABILITIES[cap.id] = cap


_COMMON_PROHIBITED = (
    "processing data the caller has no right to process",
    "attempting to trigger code execution or resource exhaustion",
)

_register(Capability(
    id="json.repair", version="1.0.0", name="JSON Repair",
    summary="Repair malformed JSON (LLM output, logs) into parseable JSON.",
    description=("Deterministically repairs almost-JSON: strips code fences and "
                 "comments, converts single quotes and Python/JS literals "
                 "(True/None/undefined), quotes bare keys, removes trailing "
                 "commas, balances brackets. Returns the parsed value plus the "
                 "exact repair steps applied. Use when a model or upstream tool "
                 "emitted JSON that json.parse rejects."),
    tags=("json", "repair", "llm-output", "parsing"),
    input_schema=_obj({"text": {"type": "string", "maxLength": 60000}}, ["text"]),
    output_schema=_obj({"ok": {"type": "boolean"}, "parsed": _ANY,
                        "repaired": {"type": "string"}, "changed": {"type": "boolean"},
                        "steps": {"type": "array", "items": {"type": "string"}}},
                       ["ok", "parsed", "repaired", "changed", "steps"]),
    run=_run_json_repair,
    fixtures=(
        {"input": {"text": "{'a': 1, 'b': True,}"},
         "expect_subset": {"ok": True, "parsed": {"a": 1, "b": True}}},
        {"input": {"text": '```json\n{"x": 1} \n```'},
         "expect_subset": {"ok": True, "parsed": {"x": 1}}},
        {"input": {"text": '{"a": [1, 2', },
         "expect_subset": {"ok": True, "parsed": {"a": [1, 2]}}},
        {"input": {"text": '{unquoted: "v", // c\n "n": None}'},
         "expect_subset": {"ok": True, "parsed": {"unquoted": "v", "n": None}}},
    ),
    failure_modes=("irreparably corrupted input returns ok=false with the best "
                   "repair attempt", "semantic ambiguity is resolved mechanically, "
                   "not by intent"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="every LLM pipeline hits malformed JSON; deterministic repair "
                      "beats re-prompting on cost and latency",
))

_register(Capability(
    id="json.validate", version="1.0.0", name="JSON Schema Validate",
    summary="Validate a JSON instance against a JSON Schema (draft 2020-12).",
    description=("Validates any JSON value against a caller-supplied JSON Schema "
                 "and returns structured errors (path + message), capped at 50. "
                 "Use before passing data across an agent boundary."),
    tags=("json", "schema", "validation"),
    input_schema=_obj({"instance": _ANY, "schema": {"type": "object"}},
                      ["instance", "schema"]),
    output_schema=_obj({"valid": {"type": "boolean"},
                        "errors": {"type": "array"},
                        "error_count": {"type": "integer"}},
                       ["valid", "errors", "error_count"]),
    run=_run_json_validate,
    fixtures=(
        {"input": {"instance": {"a": 1},
                   "schema": {"type": "object", "required": ["a"]}},
         "expect_subset": {"valid": True, "error_count": 0}},
        {"input": {"instance": {"a": "x"},
                   "schema": {"type": "object",
                              "properties": {"a": {"type": "integer"}}}},
         "expect_subset": {"valid": False, "error_count": 1}},
    ),
    failure_modes=("invalid schemas are rejected with a structured error",
                   "error list capped at 50"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="schema validation is the handshake primitive of a2a "
                      "interop; agents need it verifiable and off-model",
))

_register(Capability(
    id="json.schema_infer", version="1.0.0", name="JSON Schema Infer",
    summary="Infer a JSON Schema from example instances.",
    description=("Produces a draft-2020-12 JSON Schema generalizing one or more "
                 "example values: merged types, object properties with required "
                 "keys (present in all examples), array item schemas."),
    tags=("json", "schema", "inference"),
    input_schema=_obj({"examples": {"type": "array", "minItems": 1, "maxItems": 100}},
                      ["examples"]),
    output_schema=_obj({"schema": {"type": "object"},
                        "examples_used": {"type": "integer"}},
                       ["schema", "examples_used"]),
    run=_run_schema_infer,
    fixtures=(
        {"input": {"examples": [{"a": 1, "b": "x"}, {"a": 2}]},
         "expect_subset": {"examples_used": 2}},
    ),
    failure_modes=("inferred schemas are permissive generalizations, not "
                   "specifications",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="agents receiving unknown API payloads need a schema to "
                      "negotiate against",
))

_register(Capability(
    id="json.canonicalize", version="1.0.0", name="JSON Canonicalize (JCS)",
    summary="RFC 8785 (JCS) canonical form + sha256 of any JSON value.",
    description=("Returns the JCS-canonical serialization and its sha256. Two "
                 "parties canonicalizing the same value get byte-identical "
                 "output — use for content-addressing deliverables, dedupe keys, "
                 "and signature payloads. Same canonicalization Agent Guild uses "
                 "for its own credentials."),
    tags=("json", "canonicalization", "hashing", "jcs", "provenance"),
    input_schema=_obj({"value": _ANY}, ["value"]),
    output_schema=_obj({"canonical": {"type": "string"},
                        "sha256": {"type": "string"}, "bytes": {"type": "integer"}},
                       ["canonical", "sha256", "bytes"]),
    run=_run_canonicalize,
    fixtures=(
        {"input": {"value": {"b": 2, "a": 1}},
         "expect_subset": {"canonical": '{"a":1,"b":2}'}},
    ),
    failure_modes=("non-JSON-serializable payloads are rejected at the schema gate",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="content-addressing is the primitive under receipts and "
                      "attestations; agents need it identical cross-language",
))

_register(Capability(
    id="json.diff", version="1.0.0", name="JSON Structural Diff",
    summary="Structural diff of two JSON values with per-path changes.",
    description=("Compares two JSON values and returns added/removed/changed "
                 "paths (JSON-Pointer-style), capped at 500 changes. Use to "
                 "verify an agent's output changed only what it was asked to."),
    tags=("json", "diff", "verification"),
    input_schema=_obj({"a": _ANY, "b": _ANY}, ["a", "b"]),
    output_schema=_obj({"equal": {"type": "boolean"}, "changes": {"type": "array"},
                        "change_count": {"type": "integer"}},
                       ["equal", "changes", "change_count"]),
    run=_run_json_diff,
    fixtures=(
        {"input": {"a": {"x": 1, "y": 2}, "b": {"x": 1, "y": 3, "z": 4}},
         "expect_subset": {"equal": False, "change_count": 2}},
        {"input": {"a": [1, 2], "b": [1, 2]},
         "expect_subset": {"equal": True, "change_count": 0}},
    ),
    failure_modes=("change list capped at 500", "list diffs are positional, not "
                   "move-aware"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="output verification between agents needs a neutral, "
                      "deterministic diff",
))

_register(Capability(
    id="json.path_extract", version="1.0.0", name="JSON Path Extract",
    summary="Extract values at dotted/indexed paths from a JSON value.",
    description=("Extracts values at paths like 'items[0].name' — dotted keys "
                 "and [n] indices. Returns found/not-found per path; never "
                 "throws on a missing path. Cheaper and stricter than asking a "
                 "model to read a field."),
    tags=("json", "extraction", "path"),
    input_schema=_obj({"value": _ANY,
                       "paths": {"type": "array", "items": {"type": "string"},
                                 "minItems": 1, "maxItems": 200}},
                      ["value", "paths"]),
    output_schema=_obj({"results": {"type": "array"}}, ["results"]),
    run=_run_path_extract,
    fixtures=(
        {"input": {"value": {"items": [{"name": "a"}, {"name": "b"}]},
                   "paths": ["items[1].name", "items[9].name"]},
         "expect_subset": {"results": [
             {"path": "items[1].name", "found": True, "value": "b"},
             {"path": "items[9].name", "found": False, "value": None}]}},
    ),
    failure_modes=("no wildcard/filter syntax — literal paths only",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="field extraction from API responses is the most common "
                      "micro-task in tool pipelines",
))

_register(Capability(
    id="table.csv_to_json", version="1.0.0", name="CSV to JSON",
    summary="Parse CSV/TSV text into JSON row objects (delimiter auto-detected).",
    description=("Parses delimited text into an array of objects keyed by "
                 "header. Auto-detects , ; tab |; header optional. Deterministic "
                 "alternative to model-based table reading."),
    tags=("csv", "table", "transform"),
    input_schema=_obj({"csv": {"type": "string", "maxLength": 60000},
                       "delimiter": {"type": "string", "maxLength": 1},
                       "has_header": {"type": "boolean"}}, ["csv"]),
    output_schema=_obj({"rows": {"type": "array"}, "columns": {"type": "array"},
                        "count": {"type": "integer"}, "delimiter": {"type": "string"}},
                       ["rows", "columns", "count"]),
    run=_run_csv_to_json,
    fixtures=(
        {"input": {"csv": "name,age\nada,36\nalan,41"},
         "expect_subset": {"count": 2, "columns": ["name", "age"],
                           "rows": [{"name": "ada", "age": "36"},
                                    {"name": "alan", "age": "41"}]}},
    ),
    failure_modes=("all cells returned as strings (no type coercion)",
                   "ragged rows padded/truncated to header width"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="CSV is the lingua franca of exported data; agents "
                      "constantly need it structured",
))

_register(Capability(
    id="table.json_to_csv", version="1.0.0", name="JSON to CSV",
    summary="Serialize an array of JSON objects to CSV.",
    description=("Converts row objects to CSV with a stable, caller-controllable "
                 "column order (default: sorted union of keys). Nested values "
                 "are JSON-encoded in their cell."),
    tags=("csv", "table", "transform"),
    input_schema=_obj({"rows": {"type": "array", "minItems": 1, "maxItems": 5000,
                                "items": {"type": "object"}},
                       "columns": {"type": "array", "items": {"type": "string"}}},
                      ["rows"]),
    output_schema=_obj({"csv": {"type": "string"}, "columns": {"type": "array"},
                        "count": {"type": "integer"}}, ["csv", "columns", "count"]),
    run=_run_json_to_csv,
    fixtures=(
        {"input": {"rows": [{"a": 1, "b": "x"}, {"a": 2}]},
         "expect_subset": {"count": 2, "columns": ["a", "b"],
                           "csv": "a,b\n1,x\n2,\n"}},
    ),
    failure_modes=("missing keys become empty cells",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="the inverse transform of csv_to_json; pairs compose "
                      "into pipelines",
))

_register(Capability(
    id="table.markdown_extract", version="1.0.0", name="Markdown Table Extract",
    summary="Extract structured tables from markdown text.",
    description=("Finds GitHub-style pipe tables in markdown and returns "
                 "columns + rows per table. Use on model output or docs before "
                 "downstream structured processing."),
    tags=("markdown", "table", "extraction"),
    input_schema=_obj({"markdown": {"type": "string", "maxLength": 60000}},
                      ["markdown"]),
    output_schema=_obj({"tables": {"type": "array"}, "count": {"type": "integer"}},
                       ["tables", "count"]),
    run=_run_markdown_table,
    fixtures=(
        {"input": {"markdown": "text\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"},
         "expect_subset": {"count": 1,
                           "tables": [{"columns": ["a", "b"],
                                       "rows": [["1", "2"], ["3", "4"]],
                                       "row_count": 2}]}},
    ),
    failure_modes=("only pipe-style tables with a separator row are detected",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="models emit markdown tables; downstream tools need "
                      "arrays",
))

_register(Capability(
    id="text.date_normalize", version="1.0.0", name="Date Normalize",
    summary="Normalize arbitrary date strings to ISO 8601.",
    description=("Parses messy date strings ('3rd March 2026', '03/04/26', "
                 "'2026-03-04T10:00Z') to ISO 8601, with explicit dayfirst "
                 "control for ambiguous forms. Per-item success flags — one bad "
                 "date never fails the batch."),
    tags=("dates", "normalization", "iso8601"),
    input_schema=_obj({"dates": {"type": "array", "items": {"type": "string"},
                                 "minItems": 1, "maxItems": 500},
                       "dayfirst": {"type": "boolean"}}, ["dates"]),
    output_schema=_obj({"results": {"type": "array"}, "parsed": {"type": "integer"},
                        "failed": {"type": "integer"}},
                       ["results", "parsed", "failed"]),
    run=_run_date_normalize,
    fixtures=(
        {"input": {"dates": ["3rd March 2026", "not a date"]},
         "expect_subset": {"parsed": 1, "failed": 1}},
        {"input": {"dates": ["02/03/2026"], "dayfirst": True},
         "expect_subset": {"parsed": 1,
                           "results": [{"input": "02/03/2026", "ok": True,
                                        "iso": "2026-03-02T00:00:00",
                                        "date": "2026-03-02"}]}},
    ),
    failure_modes=("ambiguous day/month resolved by the dayfirst flag (default "
                   "month-first)", "missing components default (year required)"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="date normalization is a classic silent-failure zone for "
                      "LLMs; determinism wins",
))

_register(Capability(
    id="data.dedupe", version="1.0.0", name="Record Dedupe",
    summary="Deduplicate JSON records exactly, optionally by key subset.",
    description=("Removes duplicate records (first occurrence kept, order "
                 "preserved) using JCS-canonical equality over the whole record "
                 "or a caller-chosen key subset, optionally case-insensitive. "
                 "Reports what was removed and why."),
    tags=("data", "dedupe", "cleaning"),
    input_schema=_obj({"records": {"type": "array", "minItems": 1, "maxItems": 5000},
                       "keys": {"type": "array", "items": {"type": "string"}},
                       "case_insensitive": {"type": "boolean"}}, ["records"]),
    output_schema=_obj({"unique": {"type": "array"}, "kept": {"type": "integer"},
                        "removed": {"type": "integer"},
                        "duplicates": {"type": "array"}},
                       ["unique", "kept", "removed", "duplicates"]),
    run=_run_dedupe,
    fixtures=(
        {"input": {"records": [{"e": "A@x.com"}, {"e": "a@x.com"}, {"e": "b@x.com"}],
                   "keys": ["e"], "case_insensitive": True},
         "expect_subset": {"kept": 2, "removed": 1}},
    ),
    failure_modes=("exact/canonical matching only — no fuzzy dedupe (see "
                   "data.record_link)",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="dedupe precedes almost every aggregation an agent does",
))

_register(Capability(
    id="data.record_link", version="1.0.0", name="Record Link (fuzzy)",
    summary="Fuzzy-match records across two lists by a key field.",
    description=("Greedy best-first fuzzy matching (normalized similarity "
                 "ratio) between two record lists on chosen key fields, with a "
                 "caller-set threshold. Returns matched pairs with scores plus "
                 "unmatched indices. Entity-resolution lite: deterministic and "
                 "auditable."),
    tags=("data", "entity-resolution", "matching"),
    input_schema=_obj({"left": {"type": "array", "items": {"type": "object"},
                                "minItems": 1, "maxItems": 1000},
                       "right": {"type": "array", "items": {"type": "object"},
                                 "minItems": 1, "maxItems": 1000},
                       "left_key": {"type": "string"}, "right_key": {"type": "string"},
                       "threshold": {"type": "number", "minimum": 0.5, "maximum": 1}},
                      ["left", "right", "left_key", "right_key"]),
    output_schema=_obj({"matches": {"type": "array"},
                        "unmatched_left": {"type": "array"},
                        "unmatched_right": {"type": "array"}},
                       ["matches", "unmatched_left", "unmatched_right"]),
    run=_run_record_link,
    fixtures=(
        {"input": {"left": [{"name": "Acme Corp."}, {"name": "Zeta LLC"}],
                   "right": [{"company": "acme corp"}, {"company": "Other Inc"}],
                   "left_key": "name", "right_key": "company", "threshold": 0.8},
         "expect_subset": {"matches": [{"left_index": 0, "right_index": 0}],
                           "unmatched_left": [1], "unmatched_right": [1]}},
    ),
    failure_modes=("character-similarity only (no semantic matching)",
                   "capped at 250k comparisons"),
    prohibited_uses=_COMMON_PROHIBITED + (
        "re-identification of individuals from de-identified data",),
    demand_hypothesis="cross-source entity joins are a constant multi-agent "
                      "workflow need",
))

_register(Capability(
    id="text.regex_extract", version="1.0.0", name="Regex Extract",
    summary="Bounded, safe regex extraction over text.",
    description=("Runs a caller-supplied regular expression over text and "
                 "returns matches with groups and offsets. Guarded: pattern "
                 "length cap, nested-quantifier rejection, match-count cap — "
                 "safe to expose to strangers."),
    tags=("text", "regex", "extraction"),
    input_schema=_obj({"text": {"type": "string", "maxLength": 60000},
                       "pattern": {"type": "string", "maxLength": 300},
                       "flags": {"type": "array",
                                 "items": {"enum": ["i", "m", "s"]}},
                       "max_matches": {"type": "integer", "minimum": 1,
                                       "maximum": 1000}},
                      ["text", "pattern"]),
    output_schema=_obj({"matches": {"type": "array"}, "count": {"type": "integer"},
                        "truncated": {"type": "boolean"}},
                       ["matches", "count", "truncated"]),
    run=_run_regex_extract,
    fixtures=(
        {"input": {"text": "a@x.com, b@y.org", "pattern": r"[\w.]+@[\w.]+\.\w+"},
         "expect_subset": {"count": 2}},
        {"input": {"text": "aaaa", "pattern": r"(a+)+b"},
         "expect_error": True},
    ),
    failure_modes=("catastrophic-backtracking-shaped patterns rejected upfront",
                   "Python `re` dialect"),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="pattern extraction is cheaper here than one model call "
                      "in any pipeline",
))

_register(Capability(
    id="calc.unit_convert", version="1.0.0", name="Unit Convert",
    summary="Deterministic unit conversion (length, mass, time, data, temperature).",
    description=("Converts between units within a dimension: length (m/km/mi/ft/"
                 "in/...), mass (kg/lb/oz/...), time (ms/s/min/h/d/wk), data "
                 "(b/kb/mib/...), temperature (c/f/k). Exact factors, no model "
                 "arithmetic errors."),
    tags=("calculation", "units", "deterministic"),
    input_schema=_obj({"value": {"type": "number"}, "from": {"type": "string"},
                       "to": {"type": "string"}}, ["value", "from", "to"]),
    output_schema=_obj({"value": {"type": "number"}, "from": {"type": "string"},
                        "to": {"type": "string"}, "result": {"type": "number"},
                        "dimension": {"type": "string"}},
                       ["value", "from", "to", "result", "dimension"]),
    run=_run_unit_convert,
    fixtures=(
        {"input": {"value": 1, "from": "mi", "to": "km"},
         "expect_subset": {"result": 1.609344, "dimension": "length"}},
        {"input": {"value": 100, "from": "c", "to": "f"},
         "expect_subset": {"result": 212.0, "dimension": "temperature"}},
        {"input": {"value": 1, "from": "kg", "to": "s"}, "expect_error": True},
    ),
    failure_modes=("cross-dimension conversions rejected with a structured error",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="unit arithmetic is a known LLM failure mode; agents "
                      "want it exact",
))

_register(Capability(
    id="code.semver_compare", version="1.0.0", name="Semver Compare",
    summary="Compare semantic versions or test a version against a constraint.",
    description=("Full SemVer 2.0 precedence (including prerelease rules). "
                 "Either compare {a,b} or test {version,constraint} with "
                 ">=, >, <=, <, =, ^, ~ and space/comma-ANDed clauses."),
    tags=("code", "semver", "dependency"),
    input_schema=_obj({"a": {"type": "string"}, "b": {"type": "string"},
                       "version": {"type": "string"},
                       "constraint": {"type": "string"}}, []),
    output_schema=_obj({"a": {"type": "string"}, "b": {"type": "string"},
                        "comparison": {"type": "integer"},
                        "relation": {"type": "string"},
                        "version": {"type": "string"},
                        "constraint": {"type": "string"},
                        "satisfies": {"type": "boolean"}}, []),
    run=_run_semver,
    fixtures=(
        {"input": {"a": "1.2.3", "b": "1.2.10"},
         "expect_subset": {"comparison": -1, "relation": "a<b"}},
        {"input": {"a": "1.0.0-alpha", "b": "1.0.0"},
         "expect_subset": {"comparison": -1}},
        {"input": {"version": "1.4.2", "constraint": "^1.2.0"},
         "expect_subset": {"satisfies": True}},
        {"input": {"version": "2.0.0", "constraint": "^1.2.0"},
         "expect_subset": {"satisfies": False}},
    ),
    failure_modes=("non-semver strings rejected (no 4-part or date versions)",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="dependency reasoning shows up in most build/deploy "
                      "agent workflows",
))

_register(Capability(
    id="calc.stats", version="1.0.0", name="Descriptive Stats",
    summary="Deterministic descriptive statistics for a numeric series.",
    description=("count/sum/min/max/mean/median/stdev/variance plus arbitrary "
                 "percentiles (linear interpolation) for up to 10k numbers. "
                 "Exact arithmetic instead of model estimation."),
    tags=("calculation", "statistics", "deterministic"),
    input_schema=_obj({"values": {"type": "array", "items": {"type": "number"},
                                  "minItems": 1, "maxItems": 10000},
                       "percentiles": {"type": "array",
                                       "items": {"type": "number", "minimum": 0,
                                                 "maximum": 100}}},
                      ["values"]),
    output_schema=_obj({"count": {"type": "integer"}, "sum": {"type": "number"},
                        "min": {"type": "number"}, "max": {"type": "number"},
                        "mean": {"type": "number"}, "median": {"type": "number"},
                        "stdev": {"type": "number"}, "variance": {"type": "number"},
                        "percentiles": {"type": "object"}},
                       ["count", "mean", "median", "percentiles"]),
    run=_run_stats,
    fixtures=(
        {"input": {"values": [1, 2, 3, 4, 5], "percentiles": [50]},
         "expect_subset": {"count": 5, "mean": 3.0, "median": 3,
                           "percentiles": {"50": 3}}},
        {"input": {"values": []}, "expect_error": True},
    ),
    failure_modes=("NaN/Infinity rejected",),
    prohibited_uses=_COMMON_PROHIBITED,
    demand_hypothesis="numeric summarization off-model removes a whole class "
                      "of arithmetic hallucination",
))


# --------------------------------------------------------------------------
# execution + fixture gate
# --------------------------------------------------------------------------

def run_capability(cap_id: str, payload: dict) -> tuple[dict, float]:
    """Validate the payload against the capability's input schema, run it, and
    return (output, latency_ms). Raises KeyError (unknown), CapabilityError
    (unprocessable), jsonschema.ValidationError (bad payload)."""
    cap = CAPABILITIES[cap_id]
    jsonschema.validate(payload, cap.input_schema)
    t0 = time.perf_counter()
    out = cap.run(payload)
    return out, (time.perf_counter() - t0) * 1000.0


def _subset_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset_matches(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(_subset_matches(e, a) for e, a in zip(expected, actual)))
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return abs(float(expected) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    return expected == actual


def run_fixtures(cap: Capability) -> dict:
    """Run one capability's fixture suite. This is the PUBLISH GATE: an identity
    document is only generated for capabilities whose suite passes fully."""
    passed, failures, latencies = 0, [], []
    for i, fx in enumerate(cap.fixtures):
        try:
            out, ms = run_capability(cap.id, fx["input"])
            latencies.append(ms)
            if fx.get("expect_error"):
                failures.append({"fixture": i, "reason": "expected error, got success"})
            elif _subset_matches(fx.get("expect_subset", {}), out):
                passed += 1
            else:
                failures.append({"fixture": i, "reason": "output mismatch"})
        except (CapabilityError, jsonschema.ValidationError) as e:
            if fx.get("expect_error"):
                passed += 1
            else:
                failures.append({"fixture": i, "reason": str(e)[:200]})
        except Exception as e:  # noqa: BLE001 — a crash is a failed gate, never a crash upstream
            failures.append({"fixture": i, "reason": f"crash: {e}"[:200]})
    if any(fx.get("expect_error") for fx in cap.fixtures):
        passed_total = passed
    else:
        passed_total = passed
    return {"capability": cap.id, "version": cap.version,
            "total": len(cap.fixtures), "passed": passed_total,
            "failed": len(cap.fixtures) - passed_total,
            "ok": passed_total == len(cap.fixtures),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "failures": failures}


def validate_all() -> dict[str, dict]:
    """Run every capability's fixture suite — the swarm-wide publish gate."""
    return {cid: run_fixtures(cap) for cid, cap in sorted(CAPABILITIES.items())}


def category_of(cap_id: str) -> str:
    return cap_id.split(".", 1)[0]


# --------------------------------------------------------------------------
# flag-gated capabilities (default OFF — must not appear on any surface)
# --------------------------------------------------------------------------
# evidence.claim_check ships dark: it registers (and therefore appears on the
# REST / A2A / MCP surfaces, which are all generated from CAPABILITIES) only
# when GUILD_ENABLE_CLAIMCHECK=1 at import time. The module self-registers on
# import; see app/swarm/factcheck.py.
if os.environ.get("GUILD_ENABLE_CLAIMCHECK") == "1":
    from . import factcheck as _factcheck  # noqa: F401,E402
