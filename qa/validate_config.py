"""Validate the questionnaire and quota config before the survey loads.

Layer 1: JSON Schema (structure).  Layer 2: semantic checks (references, piping, quotas).
Usage: python qa/validate_config.py [questionnaire.json quotas.json schema.json]
"""
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft7Validator

CFG = Path(__file__).resolve().parent.parent / "config"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cond_refs(cond):
    """Yield every question id referenced inside a (possibly nested) condition."""
    if not isinstance(cond, dict):
        return
    if "q" in cond:
        yield cond["q"]
    for key in ("all", "any"):
        for sub in cond.get(key, []):
            yield from cond_refs(sub)
    if "not" in cond:
        yield from cond_refs(cond["not"])


def validate(q_path, quota_path, schema_path):
    errors = []
    questionnaire, quotas, schema = load(q_path), load(quota_path), load(schema_path)

    for err in Draft7Validator(schema).iter_errors(questionnaire):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"SCHEMA   {where}: {err.message[:150]}")
    if errors:
        return errors  # structure is broken; semantic checks would only add noise

    seen = {}
    for q in questionnaire["questions"]:
        qid = q["id"]
        if qid in seen:
            errors.append(f"DUP_ID   {qid} is defined more than once")
        for key in ("show_if", "terminate_if"):
            for ref in cond_refs(q.get(key)):
                # a terminate_if may test the question's own answer; show_if may not
                if ref == qid and key == "terminate_if":
                    continue
                if ref not in seen:
                    errors.append(f"BAD_REF  {qid}.{key} uses '{ref}', which is not defined earlier")
        src = q.get("options_from")
        if src and (src not in seen or seen[src]["type"] != "multi_select"):
            errors.append(f"BAD_PIPE {qid}.options_from '{src}' must be an earlier multi_select")
        declared = set(q.get("pipe_from", []))
        for ph in re.findall(r"\{\{(\w+)\}\}", q["text"]):
            if ph not in declared:
                errors.append(f"BAD_PIPE {qid} text uses {{{{{ph}}}}} but pipe_from does not declare it")
        for ref in declared:
            if ref not in seen:
                errors.append(f"BAD_PIPE {qid}.pipe_from '{ref}' is not defined earlier")
        seen[qid] = q

    total = quotas["total_target"]
    for name, group in quotas["groups"].items():
        src = group["source"]
        if src not in seen:
            errors.append(f"QUOTA    group '{name}' uses unknown question '{src}'")
            continue
        if sum(c["target"] for c in group["cells"]) != total:
            errors.append(f"QUOTA    group '{name}' cell targets do not sum to {total}")
        if seen[src]["type"] != "numeric":
            option_ids = {o["id"] for o in seen[src].get("options", [])}
            for cell in group["cells"]:
                if cell.get("option") not in option_ids:
                    errors.append(f"QUOTA    '{name}' cell '{cell['id']}' option not found in {src}")
        if seen[src].get("quota_group") != name:
            errors.append(f"QUOTA    {src} should declare quota_group '{name}'")
    return errors


if __name__ == "__main__":
    args = sys.argv[1:] or [CFG / "questionnaire.json", CFG / "quotas.json", CFG / "schema.json"]
    problems = validate(*args)
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("OK: config is valid")
