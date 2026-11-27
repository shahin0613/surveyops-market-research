"""Survey logic shared by the data generator and the validation pipeline (a Python mirror of engine.js)."""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
META = ["respondent_id", "status", "terminate_reason", "started_at", "ended_at", "duration_sec", "last_question"]


def load_config():
    q = json.loads((CFG / "questionnaire.json").read_text(encoding="utf-8"))
    quotas = json.loads((CFG / "quotas.json").read_text(encoding="utf-8"))
    return q["questions"], quotas


def eval_cond(c, a):
    if "all" in c: return all(eval_cond(x, a) for x in c["all"])
    if "any" in c: return any(eval_cond(x, a) for x in c["any"])
    if "not" in c: return not eval_cond(c["not"], a)
    v, op = a.get(c["q"]), c["op"]
    if op == "answered": return v not in (None, "", [], {})
    if op == "eq": return v == c["value"]
    if op == "ne": return v is not None and v != c["value"]
    if op == "in": return v in c["value"]
    if op == "contains": return isinstance(v, list) and c["value"] in v
    if not isinstance(v, (int, float)) or isinstance(v, bool): return False
    return {"gt": v > c["value"], "gte": v >= c["value"], "lt": v < c["value"], "lte": v <= c["value"]}[op]


def visible(q, answers):
    return "show_if" not in q or eval_cond(q["show_if"], answers)


def cell_for(group, answers):
    """Which quota cell does this respondent fall into (None if outside every cell)?"""
    v = answers.get(group["source"])
    for c in group["cells"]:
        if "option" in c and v == c["option"]: return c["id"]
        if "min" in c and isinstance(v, (int, float)) and c["min"] <= v <= c["max"]: return c["id"]
    return None


def columns(questions):
    cols = []
    for q in questions:
        cols += [f'{q["id"]}_{r["id"]}' for r in q["rows"]] if q["type"] == "matrix" else [q["id"]]
    return cols


def to_row(rec, questions):
    row = {k: (rec[k].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(rec.get(k), datetime) else rec.get(k, "")) for k in META}
    for q in questions:
        v = rec["answers"].get(q["id"])
        if q["type"] == "matrix":
            for r in q["rows"]:
                row[f'{q["id"]}_{r["id"]}'] = (v or {}).get(r["id"], "")
        elif isinstance(v, list):
            row[q["id"]] = "|".join(v)
        else:
            row[q["id"]] = "" if v is None else v
    return row


def from_row(row, questions):
    a = {}
    for q in questions:
        if q["type"] == "matrix":
            d = {r["id"]: row[f'{q["id"]}_{r["id"]}'] for r in q["rows"] if row.get(f'{q["id"]}_{r["id"]}', "") != ""}
            if d: a[q["id"]] = d
            continue
        raw = row.get(q["id"], "")
        if raw == "": continue
        if q["type"] == "multi_select":
            a[q["id"]] = raw.split("|")
        elif q["type"] == "numeric":
            try: a[q["id"]] = int(float(raw))
            except ValueError: a[q["id"]] = raw
        else:
            a[q["id"]] = raw
    return a