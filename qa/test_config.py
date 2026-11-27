"""The config validator must accept the shipped config and catch broken edits."""
import json
from pathlib import Path

import validate_config as vc

CFG = Path(__file__).resolve().parent.parent / "config"


def check(tmp_path, edit_q=None, edit_quotas=None):
    q = json.loads((CFG / "questionnaire.json").read_text(encoding="utf-8"))
    r = json.loads((CFG / "quotas.json").read_text(encoding="utf-8"))
    if edit_q: edit_q(q)
    if edit_quotas: edit_quotas(r)
    (tmp_path / "q.json").write_text(json.dumps(q))
    (tmp_path / "r.json").write_text(json.dumps(r))
    return vc.validate(tmp_path / "q.json", tmp_path / "r.json", CFG / "schema.json")


def question(q, qid):
    return next(x for x in q["questions"] if x["id"] == qid)


def test_shipped_config_is_valid(tmp_path):
    assert check(tmp_path) == []

def test_unknown_skip_target_is_caught(tmp_path):
    errs = check(tmp_path, edit_q=lambda q: question(q, "Q6")["show_if"].update(q="Q99"))
    assert any("Q99" in e for e in errs)

def test_undeclared_pipe_is_caught(tmp_path):
    errs = check(tmp_path, edit_q=lambda q: question(q, "Q5")["pipe_from"].clear())
    assert any("BAD_PIPE" in e for e in errs)

def test_quota_total_mismatch_is_caught(tmp_path):
    errs = check(tmp_path, edit_quotas=lambda r: r["groups"]["city_tier"]["cells"][0].update(target=1))
    assert any("QUOTA" in e for e in errs)

def test_invalid_question_type_is_caught(tmp_path):
    errs = check(tmp_path, edit_q=lambda q: question(q, "Q1").update(type="dropdown"))
    assert any("SCHEMA" in e for e in errs)