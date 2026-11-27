"""SurveyOps data pipeline: validate raw responses, flag bad data, export a clean dataset and cross-tabs.

Run:  python pipeline/validate.py
Reads  data/sample_responses.csv (+ data/planted_errors.csv to score itself, if present)
Writes data/clean_responses.csv, data/flagged_responses.csv, data/surveyops_analysis.db,
       reports/data_quality_report.json, reports/crosstabs.xlsx, reports/research_summary.md
All rules are driven by config/questionnaire.json, so a changed questionnaire changes the checks.
"""
import csv
import json
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from survey_logic import META, ROOT, cell_for, columns, eval_cond, from_row, load_config, to_row, visible

DATA, REPORTS = ROOT / "data", ROOT / "reports"
SPEED_RATIO = 0.4  # faster than 40% of the typical time for the same route = speeder
DUP_WINDOW = timedelta(minutes=15)  # identical answers this close together = the same person submitting twice
EXPECTED = {  # which rules count as "caught" for each planted error type
    "speeder": {"speeder"}, "straightliner": {"straightlining"}, "duplicate": {"duplicate"},
    "routing": {"routing_violation"}, "contradiction": {"contradiction"},
    "invalid_value": {"invalid_option", "ineligible_complete", "out_of_range"}, "missing_required": {"missing_required"},
}


def select_flags(q, values, answers, by_id):
    src = by_id[q["options_from"]] if q.get("options_from") else q
    base = {o["id"] for o in src["options"]}
    offered = set(answers.get(q["options_from"], [])) if q.get("options_from") else base
    out = []
    for x in values:
        if x not in base:
            out.append(("invalid_option", f"{q['id']}='{x}' is not a valid answer code"))
        elif x not in offered:
            out.append(("contradiction", f"{q['id']}='{x}' was not among the {q['options_from']} selections"))
    return out


def check_complete(answers, questions, by_id):
    flags = []
    for q in questions:
        qid, v, shown = q["id"], answers.get(q["id"]), visible(q, answers)
        if v is None:
            if shown and q["required"]:
                flags.append(("missing_required", f"{qid} was shown but not answered"))
            continue
        if not shown:
            flags.append(("routing_violation", f"{qid} answered but should have been skipped"))
            continue
        t = q["type"]
        if t == "numeric":
            if not isinstance(v, int) or not q["min"] <= v <= q["max"]:
                flags.append(("out_of_range", f"{qid}={v}"))
            elif "terminate_if" in q and eval_cond(q["terminate_if"], answers):
                flags.append(("ineligible_complete", f"{qid}={v} should have ended the survey ({q['terminate_reason']})"))
        elif t == "single_select":
            flags += select_flags(q, [v], answers, by_id)
        elif t == "multi_select":
            flags += select_flags(q, v, answers, by_id)
            if len(v) > 1 and any(o.get("exclusive") and o["id"] in v for o in q["options"]):
                flags.append(("contradiction", f"{qid} combines an exclusive option with others"))
        elif t == "matrix":
            scale = {s["id"] for s in q["scale"]}
            if any(x not in scale for x in v.values()):
                flags.append(("invalid_option", f"{qid} has a rating outside the scale"))
            elif len(v) == len(q["rows"]) and len(set(v.values())) == 1:
                flags.append(("straightlining", f"{qid} gives the same rating ({next(iter(v.values()))}) to every row"))
        elif t == "text" and len(v) > q["max_length"]:
            flags.append(("out_of_range", f"{qid} is longer than {q['max_length']} characters"))
    return flags


def run_sql(con, text):
    out = {}
    for block in re.split(r"^-- name:\s*", text, flags=re.M)[1:]:
        name, _, sql = block.partition("\n")
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        out[name.strip()] = [dict(zip(cols, r)) for r in cur.fetchall()]
    return out


def main():
    questions, quotas = load_config()
    by_id = {q["id"]: q for q in questions}
    age_g, city_g = quotas["groups"]["age_band"], quotas["groups"]["city_tier"]

    with open(DATA / "sample_responses.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    recs = [{**{k: r[k] for k in META}, "answers": from_row(r, questions)} for r in rows]
    complete = [r for r in recs if r["status"] == "complete"]

    # 1. Per-respondent checks
    flags = defaultdict(list)
    for r in complete:
        for rule, detail in check_complete(r["answers"], questions, by_id):
            flags[r["respondent_id"]].append((rule, detail))

    # 2. Speeders: compared with the median time for the same route (streamers take longer than non-streamers)
    durations = defaultdict(list)
    for r in complete:
        durations[r["answers"].get("Q1")].append(float(r["duration_sec"]))
    medians = {k: statistics.median(v) for k, v in durations.items()}
    for r in complete:
        d, m = float(r["duration_sec"]), medians[r["answers"].get("Q1")]
        if d < SPEED_RATIO * m:
            flags[r["respondent_id"]].append(("speeder", f"{d:.0f}s against a typical {m:.0f}s"))

    # 3. Duplicates: identical answers submitted within minutes of each other (keep the first).
    #    Identical answers hours or days apart are coincidence on short routes, so they are not flagged.
    last = {}
    for r in sorted(complete, key=lambda r: r["started_at"]):
        t = datetime.strptime(r["started_at"], "%Y-%m-%dT%H:%M:%SZ")
        fp = json.dumps(r["answers"], sort_keys=True)
        prev = last.get(fp)
        if prev and t - prev[0] <= DUP_WINDOW:
            flags[r["respondent_id"]].append(("duplicate", f"identical answers to {prev[1]} within {int((t - prev[0]).total_seconds())}s"))
        else:
            last[fp] = (t, r["respondent_id"])

    clean = [r for r in complete if r["respondent_id"] not in flags]

    # 4. Outputs: CSVs
    fields = META + columns(questions)
    with open(DATA / "clean_responses.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerows(to_row(r, questions) for r in clean)
    with open(DATA / "flagged_responses.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["respondent_id", "rules", "details"])
        for rid, fl in sorted(flags.items()):
            w.writerow([rid, "; ".join(x[0] for x in fl), "; ".join(x[1] for x in fl)])

    # 5. SQLite tables, then the SQL analysis
    db = DATA / "surveyops_analysis.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE raw_responses (respondent_id TEXT, status TEXT, terminate_reason TEXT, started_at TEXT,
            duration_sec INTEGER, age INTEGER, age_band TEXT, city_tier TEXT, last_question TEXT);
        CREATE TABLE flags (respondent_id TEXT, rule TEXT, detail TEXT);
        CREATE TABLE clean_responses (respondent_id TEXT, age INTEGER, age_band TEXT, city_tier TEXT,
            uses_streaming TEXT, most_used TEXT, hours TEXT, willing_to_pay TEXT, duration_sec INTEGER);
        CREATE TABLE clean_platforms (respondent_id TEXT, platform TEXT);""")
    for r in recs:
        a = r["answers"]
        age = a.get("S1") if isinstance(a.get("S1"), int) else None
        con.execute("INSERT INTO raw_responses VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["respondent_id"], r["status"], r["terminate_reason"], r["started_at"], int(r["duration_sec"]),
                     age, cell_for(age_g, a), cell_for(city_g, a), r["last_question"]))
    for rid, fl in flags.items():
        con.executemany("INSERT INTO flags VALUES (?,?,?)", [(rid, rule, detail) for rule, detail in fl])
    for r in clean:
        a = r["answers"]
        con.execute("INSERT INTO clean_responses VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["respondent_id"], a["S1"], cell_for(age_g, a), cell_for(city_g, a), a.get("Q1"), a.get("Q4"),
                     a.get("Q5"), a.get("Q9"), int(r["duration_sec"])))
        con.executemany("INSERT INTO clean_platforms VALUES (?,?)", [(r["respondent_id"], p) for p in a.get("Q2", [])])
    con.commit()
    sql = run_sql(con, (ROOT / "pipeline" / "analysis.sql").read_text(encoding="utf-8"))
    con.close()

    # 6. Quota fill from the clean data
    quota = {}
    for name, key, rows_ in (("age_band", "age_band", sql["completes_by_age_band"]), ("city_tier", "city_tier", sql["completes_by_city_tier"])):
        got = {x[key]: x["completes"] for x in rows_}
        quota[name] = [{"cell": c["id"], "target": c["target"], "completes": got.get(c["id"], 0),
                        "pct": round(100 * got.get(c["id"], 0) / c["target"], 1)} for c in quotas["groups"][name]["cells"]]

    # 7. Score the pipeline against the answer key
    evaluation = None
    key_path = DATA / "planted_errors.csv"
    if key_path.exists():
        with open(key_path, newline="", encoding="utf-8") as f:
            planted = {r["respondent_id"]: r["error_type"] for r in csv.DictReader(f)}
        per_type = defaultdict(lambda: {"planted": 0, "caught": 0})
        missed = []
        for rid, kind in planted.items():
            per_type[kind]["planted"] += 1
            if {x[0] for x in flags.get(rid, [])} & EXPECTED[kind]:
                per_type[kind]["caught"] += 1
            else:
                missed.append({"respondent_id": rid, "error_type": kind})
        false_pos = [rid for rid in flags if rid not in planted]
        evaluation = {"planted": len(planted), "caught": len(planted) - len(missed), "per_type": dict(per_type),
                      "missed": missed, "false_positives": false_pos}

    # 8. Cross-tabs (Excel) and the short research summary
    df = pd.DataFrame([{"band": cell_for(age_g, r["answers"]), "city": cell_for(city_g, r["answers"]), "Q1": r["answers"].get("Q1"),
                        "Q2": r["answers"].get("Q2", []), "Q4": r["answers"].get("Q4"), "Q9": r["answers"].get("Q9")} for r in clean])
    st = df[df.Q1 == "yes"]
    bands = [c["id"] for c in age_g["cells"]]
    plat = {o["id"]: o["label"] for o in by_id["Q2"]["options"]}
    wtp = {o["id"]: o["label"] for o in by_id["Q9"]["options"]}
    usage = pd.DataFrame({b: {lab: round(100 * sum(pid in p for p in st[st.band == b].Q2) / max(1, (st.band == b).sum()), 1)
                              for pid, lab in plat.items()} for b in bands})
    usage.index.name = "Platform used (% of streamers)"
    base_n = {b: int((st.band == b).sum()) for b in bands}
    most = (pd.crosstab(st.band, st.Q4, normalize="index") * 100).round(1).rename(columns=plat)
    pay = (pd.crosstab(st.city, st.Q9, normalize="index") * 100).round(1).reindex(columns=list(wtp)).rename(columns=wtp)
    quality = pd.DataFrame(sql["flags_by_rule"]).rename(columns={"rule": "Check", "flagged": "Records flagged"})
    with pd.ExcelWriter(REPORTS / "crosstabs.xlsx", engine="openpyxl") as xl:
        usage.to_excel(xl, sheet_name="Platform by age band")
        pd.DataFrame({"Streamers in base (n)": base_n}).T.to_excel(xl, sheet_name="Platform by age band", startrow=len(usage) + 3)
        most.to_excel(xl, sheet_name="Most used by age band")
        pay.to_excel(xl, sheet_name="Max monthly spend by city")
        quality.to_excel(xl, sheet_name="Data quality", index=False)
        for ws in xl.book.worksheets:
            ws.column_dimensions["A"].width = 34
            for col in "BCDEFG": ws.column_dimensions[col].width = 18

    top = usage.mean(axis=1).idxmax()
    gap = usage.loc[plat["netflix"], "18-24"] - usage.loc[plat["netflix"], "35-45"]
    hi = [wtp[k] for k in ("200to399", "gt400")]
    spend = {b: round(float(pay_b), 1) for b, pay_b in (
        (b, 100 * st[st.band == b].Q9.isin(["200to399", "gt400"]).mean()) for b in bands)}
    summary = [
        "# Streaming habits in India: research summary",
        f"*Simulated study. Base: {len(clean)} valid completes ({len(st)} streaming users) after removing "
        f"{len(flags)} records that failed quality checks.*", "",
        f"- **{top}** is the most widely used platform across all three age bands.",
        f"- Netflix use falls from {usage.loc[plat['netflix'], '18-24']}% of streamers aged 18-24 to "
        f"{usage.loc[plat['netflix'], '35-45']}% of those aged 35-45, a gap of {gap:.1f} percentage points.",
        "- Most-used platform by age band: " + "; ".join(f"{b}: {most.loc[b].idxmax()}" for b in bands) + ".",
        "- Share willing to pay at least Rs 200 a month: " + "; ".join(f"{b}: {v}%" for b, v in spend.items()) + ".",
        "", "*Independent simulation for portfolio purposes, not real survey data.*"]
    (REPORTS / "research_summary.md").write_text("\n".join(summary), encoding="utf-8")

    # 9. Report for the dashboard
    status = {x["status"]: x["responses"] for x in sql["status_counts"]}
    report = {"records": len(recs), "status_counts": status, "flagged_completes": len(flags), "clean_completes": len(clean),
              "dropout_pct": sql["dropout_rate"][0]["dropout_pct"], "quota": quota, "warn_at": quotas["warn_at"],
              "median_seconds": medians, "evaluation": evaluation, "sql": sql}
    (REPORTS / "data_quality_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Records read: {len(recs)} {status}")
    print(f"Complete records failing a check: {len(flags)} -> clean dataset of {len(clean)} completes")
    print("Flags by rule: " + ", ".join(f"{x['rule']} {x['flagged']}" for x in sql["flags_by_rule"]))
    if evaluation:
        print(f"\nPlanted errors caught: {evaluation['caught']}/{evaluation['planted']}")
        for kind, v in sorted(evaluation["per_type"].items()):
            print(f"  {kind:<17}{v['caught']}/{v['planted']}")
        print(f"False positives (genuine respondents flagged): {len(evaluation['false_positives'])}")
    print(f"Dropout rate: {report['dropout_pct']}%")
    for name, cells in quota.items():
        print(f"Quota {name}: " + ", ".join(f"{c['cell']} {c['completes']}/{c['target']} ({c['pct']}%)" for c in cells))


if __name__ == "__main__":
    main()
