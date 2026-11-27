"""Browser tests: a real Chromium clicks through the real survey served by the real server."""
import json
import shutil
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import expect

from conftest import ROOT, start_server

MATRIX = {"price": "3", "variety": "4", "regional": "5", "quality": "4", "adfree": "2"}


class Flow:
    """Drives the survey the way a respondent would."""

    def __init__(self, page, base, seed=1):
        self.page = page
        page.goto(f"{base}/survey/?seed={seed}")
        self.at("S1")

    def form(self): return self.page.locator("form.card")
    def at(self, qid): expect(self.form()).to_have_attribute("data-qid", qid)
    def next(self): self.page.click("[data-testid=next]")
    def error(self): return self.page.locator("[data-testid=error]")

    def number(self, v):
        self.page.fill("form.card input", str(v))
        self.next()

    text = number

    def choose(self, *ids):
        for i in ids:
            self.page.check(f'form.card input[value="{i}"]')
        self.next()

    def rate(self):
        for row, val in MATRIX.items():
            self.page.check(f'input[name="Q8.{row}"][value="{val}"]')

    def options(self):
        return self.page.locator("form.card input").evaluate_all("els => els.map(e => e.value)")

    def payload(self):
        return json.loads(self.page.locator("[data-testid=payload]").text_content())

    def ended(self, status, reason=None):
        expect(self.page.locator("[data-testid=end]")).to_have_attribute("data-status", status)
        if reason:
            assert self.payload()["terminate_reason"] == reason


def walk(f, platforms=("prime",), stop_at=None):
    """Answers the survey in order, optionally stopping when a given question is on screen."""
    steps = [
        ("S1", lambda: f.number(25)), ("S2", lambda: f.choose("metro")), ("Q1", lambda: f.choose("yes")),
        ("Q2", lambda: f.choose(*platforms)), ("Q3", lambda: f.text("Aha")), ("Q4", lambda: f.choose(platforms[0])),
        ("Q5", lambda: f.choose("2to5")), ("Q6", lambda: f.choose("own")), ("Q7", lambda: f.choose("friends")),
        ("Q8", lambda: (f.rate(), f.next())), ("Q9", lambda: f.choose("100to199")),
    ]
    shown = {"Q3": "other" in platforms, "Q6": "netflix" in platforms}
    for qid, act in steps:
        if not shown.get(qid, True):
            continue
        f.at(qid)
        if qid == stop_at:
            return
        act()
    f.ended("complete")


@pytest.fixture
def flow(page, server):
    return lambda seed=1: Flow(page, server.base, seed)


def blocked(f, qid):
    f.next()
    f.at(qid)
    expect(f.error()).not_to_have_text("")


# ---- Screening and termination ----
@pytest.mark.parametrize("age", [17, 46])
def test_age_outside_18_to_45_is_terminated(flow, age):
    f = flow(); f.number(age); f.ended("screened_out", "age_out_of_range")

@pytest.mark.parametrize("age", [18, 45])
def test_age_boundaries_are_eligible(flow, age):
    f = flow(); f.number(age); f.at("S2")

def test_living_outside_india_is_terminated(flow):
    f = flow(); f.number(25); f.at("S2"); f.choose("outside_india"); f.ended("screened_out", "outside_india")


# ---- Routing and piping ----
def test_non_streamer_skips_platform_questions(flow):
    f = flow(); walk(f, stop_at="Q1"); f.choose("no"); f.at("Q10"); f.choose("cost"); f.ended("complete")
    assert list(f.payload()["answers"]) == ["S1", "S2", "Q1", "Q10"]

def test_streamer_without_netflix_or_other_skips_q3_q6_q10(flow):
    f = flow(); walk(f, ("prime",))
    assert not {"Q3", "Q6", "Q10"} & set(f.payload()["answers"])

def test_netflix_triggers_payment_question(flow):
    f = flow(); walk(f, ("netflix",), stop_at="Q4"); f.choose("netflix"); f.at("Q5"); f.choose("lt2"); f.at("Q6")

def test_other_triggers_specify_question(flow):
    f = flow(); walk(f, ("other",), stop_at="Q3")

def test_most_used_options_come_from_selected_platforms(flow):
    f = flow(); walk(f, ("zee5", "prime"), stop_at="Q4")
    assert sorted(f.options()) == ["prime", "zee5"]

def test_hours_question_pipes_chosen_platform(flow):
    f = flow(); walk(f, ("zee5", "prime"), stop_at="Q4"); f.choose("zee5"); f.at("Q5")
    expect(f.page.locator("h1.question")).to_contain_text("ZEE5")


# ---- Validation ----
def test_empty_age_is_blocked(flow):
    blocked(flow(), "S1")

@pytest.mark.parametrize("bad", ["17.5", "5", "150"])
def test_invalid_age_is_blocked(flow, bad):
    f = flow(); f.page.fill("form.card input", bad); blocked(f, "S1")

def test_unanswered_single_select_is_blocked(flow):
    f = flow(); walk(f, stop_at="Q1"); blocked(f, "Q1")

def test_unanswered_multi_select_is_blocked(flow):
    f = flow(); walk(f, stop_at="Q2"); blocked(f, "Q2")

def test_empty_text_answer_is_blocked(flow):
    f = flow(); walk(f, ("other",), stop_at="Q3"); blocked(f, "Q3")

def test_partially_rated_matrix_is_blocked(flow):
    f = flow(); walk(f, stop_at="Q8")
    f.page.check('input[name="Q8.price"][value="3"]'); blocked(f, "Q8")


# ---- Option behaviour ----
def test_none_of_these_is_exclusive(flow):
    f = flow(); walk(f, stop_at="Q7")
    f.page.check('input[value="social"]'); f.page.check('input[value="none"]')
    expect(f.page.locator('input[value="social"]')).not_to_be_checked()
    f.page.check('input[value="ads"]')
    expect(f.page.locator('input[value="none"]')).not_to_be_checked()

def test_anchored_options_stay_last(flow):
    f = flow(); walk(f, stop_at="Q2")
    assert f.options()[-1] == "other"

def _order(flow, seed):
    f = flow(seed); walk(f, stop_at="Q2"); return f.options()

def test_same_seed_gives_same_option_order(flow):
    assert _order(flow, 7) == _order(flow, 7)

def test_different_seeds_change_option_order(flow):
    assert len({tuple(_order(flow, s)) for s in (1, 2, 3, 4)}) > 1


# ---- Data capture ----
def test_completed_response_has_answers_and_timings(flow):
    f = flow(); walk(f, ("netflix", "other")); p = f.payload()
    assert p["status"] == "complete" and p["survey_id"] == "streaming-habits-india"
    assert set(p["answers"]) == set(p["timings_ms"]) and {"Q3", "Q6"} <= set(p["answers"])

def test_completed_response_is_saved_to_database(page, fresh_server):
    walk(Flow(page, fresh_server.base))
    expect(page.locator("[data-testid=save-status]")).to_have_text("Your responses have been recorded.")
    assert fresh_server.rows("complete") == 1

def test_leaving_midway_is_recorded_as_abandoned(page, fresh_server):
    f = Flow(page, fresh_server.base); f.number(25); f.at("S2")
    page.goto("about:blank")
    for _ in range(25):
        if fresh_server.rows("abandoned") == 1:
            break
        time.sleep(0.2)
    assert fresh_server.rows("abandoned") == 1


# ---- Quotas ----
def seed(server, n, age, city, tag="s"):
    for i in range(n):
        body = json.dumps({"respondent_id": f"{tag}{age}{city}{i}", "survey_id": "x", "status": "complete",
                           "answers": {"S1": age, "S2": city}}).encode()
        urllib.request.urlopen(urllib.request.Request(
            server.base + "/api/responses", body, {"Content-Type": "application/json"})).read()

def cell(server, group, cell_id):
    data = json.loads(urllib.request.urlopen(server.base + "/api/quota-status").read())
    return next(c for c in data["groups"][group]["cells"] if c["id"] == cell_id)

def test_full_age_quota_stops_only_that_age_group(page, fresh_server):
    seed(fresh_server, 120, 40, "metro")
    Flow(page, fresh_server.base).number(40)
    expect(page.locator("[data-testid=end]")).to_have_attribute("data-status", "quota_full")
    f = Flow(page, fresh_server.base); f.number(25); f.at("S2")

def test_full_city_quota_stops_only_that_city(page, fresh_server):
    seed(fresh_server, 120, 22, "tier3")
    f = Flow(page, fresh_server.base); f.number(25); f.at("S2"); f.choose("tier3"); f.ended("quota_full")
    f = Flow(page, fresh_server.base); f.number(25); f.at("S2"); f.choose("metro"); f.at("Q1")

def test_quota_warning_appears_at_90_percent(fresh_server):
    seed(fresh_server, 107, 40, "metro")
    assert not cell(fresh_server, "age_band", "35-45")["warning"]
    seed(fresh_server, 1, 40, "metro", tag="x")
    c = cell(fresh_server, "age_band", "35-45")
    assert c["warning"] and not c["full"]

def test_duplicate_respondent_is_rejected(fresh_server):
    seed(fresh_server, 1, 25, "metro")
    with pytest.raises(urllib.error.HTTPError) as err:
        seed(fresh_server, 1, 25, "metro")
    assert err.value.code == 409


# ---- The headline claim: the JSON controls the survey ----
def test_editing_the_json_changes_the_survey(page, tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    q = json.loads((cfg / "questionnaire.json").read_text(encoding="utf-8"))
    q["questions"][0]["terminate_if"]["any"][1]["value"] = 50  # upper age limit 45 -> 50
    (cfg / "questionnaire.json").write_text(json.dumps(q), encoding="utf-8")
    server = start_server(tmp_path, cfg)
    try:
        f = Flow(page, server.base); f.number(48); f.at("S2")
    finally:
        server.stop()