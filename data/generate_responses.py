"""Generates 600 simulated survey records (500 completes) with 50 deliberately planted data errors.

Run:  python data/generate_responses.py
Writes data/sample_responses.csv (the raw data) and data/planted_errors.csv (the answer key used to score the pipeline).
The respondents are built by walking config/questionnaire.json, so the data always follows the survey's own routing.
"""
import copy
import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from survey_logic import META, ROOT, columns, load_config, to_row, visible  # noqa: E402

rng = random.Random(2026)
questions, quotas = load_config()
QBY = {q["id"]: q for q in questions}
AGE_CELLS = quotas["groups"]["age_band"]["cells"]
CITY_CELLS = quotas["groups"]["city_tier"]["cells"]

P_STREAM = {"18-24": 0.92, "25-34": 0.88, "35-45": 0.82}
PLATFORM_P = {
    "18-24": {"netflix": .55, "prime": .50, "hotstar": .45, "jiocinema": .40, "sonyliv": .15, "zee5": .12, "youtube": .90, "other": .05},
    "25-34": {"netflix": .50, "prime": .55, "hotstar": .50, "jiocinema": .40, "sonyliv": .20, "zee5": .20, "youtube": .85, "other": .05},
    "35-45": {"netflix": .30, "prime": .45, "hotstar": .55, "jiocinema": .35, "sonyliv": .30, "zee5": .35, "youtube": .75, "other": .04},
}
OTHER_PLATFORMS = ["MX Player", "Hoichoi", "Sun NXT", "Lionsgate Play", "ALTBalaji"]
WEIGHTS = {
    "Q5": {"lt2": .25, "2to5": .40, "6to10": .25, "gt10": .10},
    "Q6": {"own": .45, "family": .25, "friends": .20, "bundle": .10},
    "Q10": {"cost": .35, "time": .20, "tv": .20, "internet": .15, "interest": .10},
}
WTP = {"18-24": [.40, .40, .15, .05], "25-34": [.25, .40, .25, .10], "35-45": [.15, .35, .33, .17]}
ROW_MEAN = {"price": 4.2, "variety": 3.8, "regional": 3.4, "quality": 3.9, "adfree": 3.0}
BASE_MS = {"numeric": 4500, "single_select": 5000, "multi_select": 8500, "text": 9000, "matrix": 22000}
_n = 0


def pick(w): return rng.choices(list(w), weights=list(w.values()))[0]
def next_id():
    global _n
    _n += 1
    return f"R{_n:05d}"
def band_of(age): return next(c["id"] for c in AGE_CELLS if c["min"] <= age <= c["max"])
def ms(q): return round(BASE_MS[q["type"]] * max(0.6, min(2.2, rng.lognormvariate(0, 0.25))))


def sample_age_city():
    cell = rng.choices(AGE_CELLS, weights=[c["target"] for c in AGE_CELLS])[0]
    city = rng.choices(CITY_CELLS, weights=[c["target"] for c in CITY_CELLS])[0]["option"]
    return rng.randint(cell["min"], cell["max"]), city


def start_time():
    hour = rng.choices(range(8, 24), weights=[1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4, 5, 5, 4, 3, 2])[0]
    return datetime(2026, 11, 10) + timedelta(days=rng.randrange(14), hours=hour, minutes=rng.randrange(60), seconds=rng.randrange(60))


def rate_matrix(q):
    d = {r["id"]: str(max(1, min(5, round(rng.gauss(ROW_MEAN[r["id"]], 1.0))))) for r in q["rows"]}
    if len(set(d.values())) == 1:  # genuine respondents do not straightline
        k = rng.choice(list(d)); d[k] = "1" if d[k] != "1" else "5"
    return d


def make_answers(age, city, force_streamer=False):
    """Walks the questionnaire exactly like the survey engine does."""
    band, a, t = band_of(age), {}, {}
    for q in questions:
        if not visible(q, a):
            continue
        qid = q["id"]
        if qid == "S1": v = age
        elif qid == "S2": v = city
        elif qid == "Q1": v = "yes" if force_streamer or rng.random() < P_STREAM[band] else "no"
        elif qid == "Q2":
            v = [p for p, pr in PLATFORM_P[band].items() if rng.random() < pr] or ["youtube"]
        elif qid == "Q3": v = rng.choice(OTHER_PLATFORMS)
        elif qid == "Q4": v = rng.choice(a["Q2"])
        elif qid == "Q7":
            opts = [o["id"] for o in q["options"] if not o.get("exclusive")]
            v = ["none"] if rng.random() < 0.10 else rng.sample(opts, rng.randint(1, 3))
        elif qid == "Q8": v = rate_matrix(q)
        elif qid == "Q9": v = rng.choices([o["id"] for o in q["options"]], weights=WTP[band])[0]
        else: v = pick(WEIGHTS[qid])
        a[qid], t[qid] = v, ms(q)
    return a, t


def record(status, answers, timings, reason="", duration=None):
    start = start_time()
    dur = duration if duration is not None else round(sum(timings.values()) / 1000 + rng.uniform(1, 4))
    return {"respondent_id": next_id(), "status": status, "terminate_reason": reason, "started_at": start,
            "ended_at": start + timedelta(seconds=dur), "duration_sec": dur, "answers": answers, "timings": timings,
            "last_question": list(answers)[-1] if answers else ""}


# ---- Planted errors: each takes a genuine streamer and breaks exactly one thing ----
def plant(kind):
    age, city = sample_age_city()
    a, t = make_answers(age, city, force_streamer=True)
    dur = None
    if kind == "speeder":
        dur = round(rng.uniform(12, 32))
    elif kind == "straightliner":
        v = rng.choice("135")
        a["Q8"] = {r["id"]: v for r in QBY["Q8"]["rows"]}
    elif kind == "routing":
        if "netflix" not in a["Q2"] and rng.random() < 0.5: a["Q6"] = "own"
        else: a["Q10"] = "cost"
    elif kind == "contradiction":
        a["Q4"] = rng.choice([o["id"] for o in QBY["Q2"]["options"] if o["id"] not in a["Q2"]])
    elif kind == "invalid_value":
        if rng.random() < 0.5: a["S1"] = rng.randint(46, 60)
        else: a["Q9"] = "999"
    elif kind == "missing_required":
        k = rng.choice(["Q5", "Q7", "Q9"]); del a[k]; del t[k]
    return record("complete", a, t, duration=dur)


def main():
    records, planted = [], []
    legit = []
    for _ in range(450):
        legit.append(record("complete", *make_answers(*sample_age_city())))
    records += legit

    plan = (["speeder"] * 10 + ["straightliner"] * 10 + ["routing"] * 8 + ["contradiction"] * 6
            + ["invalid_value"] * 4 + ["missing_required"] * 4)
    for kind in plan:
        r = plant(kind); records.append(r); planted.append((r["respondent_id"], kind))
    for orig in rng.sample(legit, 8):  # the same person submitting twice
        dup = copy.deepcopy(orig)
        dup["respondent_id"] = next_id()
        dup["started_at"] = orig["started_at"] + timedelta(seconds=rng.randint(30, 180))
        dup["ended_at"] = dup["started_at"] + timedelta(seconds=dup["duration_sec"])
        records.append(dup); planted.append((dup["respondent_id"], "duplicate"))

    for i in range(60):  # screened out
        if i % 2 == 0:
            a = {"S1": rng.choice(list(range(14, 18)) + list(range(46, 61)))}; reason = "age_out_of_range"
        else:
            a = {"S1": sample_age_city()[0], "S2": "outside_india"}; reason = "outside_india"
        t = {k: ms(QBY[k]) for k in a}
        records.append(record("screened_out", a, t, reason))
    for _ in range(40):  # abandoned part-way
        a, t = make_answers(*sample_age_city())
        keep = list(a)[: rng.randint(1, len(a) - 1)]
        a, t = {k: a[k] for k in keep}, {k: t[k] for k in keep}
        records.append(record("abandoned", a, t))

    records.sort(key=lambda r: r["started_at"])
    fields = META + columns(questions)
    with open(ROOT / "data" / "sample_responses.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerows(to_row(r, questions) for r in records)
    with open(ROOT / "data" / "planted_errors.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["respondent_id", "error_type"]); w.writerows(planted)
    counts = {s: sum(r["status"] == s for r in records) for s in ("complete", "screened_out", "abandoned")}
    print(f"Wrote {len(records)} records {counts} with {len(planted)} planted errors")


if __name__ == "__main__":
    main()