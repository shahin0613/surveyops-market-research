"""SurveyOps server: serves the survey, tracks quotas and stores responses in SQLite.

Run:    python server/app.py          then open http://localhost:8000/survey/
Reset:  python server/app.py --reset  (deletes all stored responses)
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("SURVEYOPS_DB", ROOT / "db" / "surveyops.db"))
CONFIG_DIR = Path(os.environ.get("SURVEYOPS_CONFIG", ROOT / "config"))
STATUSES = {"complete", "screened_out", "quota_full", "abandoned"}
app = Flask(__name__)


def run(sql, args=(), write=False):
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                respondent_id TEXT PRIMARY KEY, survey_id TEXT NOT NULL, version TEXT,
                status TEXT NOT NULL, terminate_reason TEXT, started_at TEXT, ended_at TEXT,
                duration_sec INTEGER, quota_cells TEXT, answers TEXT NOT NULL, timings_ms TEXT,
                received_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
        )
        rows = con.execute(sql, args).fetchall()
        if write:
            con.commit()
        return rows
    finally:
        con.close()


def load_quotas():
    return json.loads((CONFIG_DIR / "quotas.json").read_text(encoding="utf-8"))


def cell_for(group, answers):
    """Which quota cell does this respondent fall into? Works for option cells and numeric bands."""
    value = answers.get(group["source"])
    for cell in group["cells"]:
        if "option" in cell and value == cell["option"]:
            return cell["id"]
        if "min" in cell and isinstance(value, (int, float)) and cell["min"] <= value <= cell["max"]:
            return cell["id"]
    return None


def quota_status():
    quotas = load_quotas()
    out = {"total_target": quotas["total_target"], "warn_at": quotas["warn_at"], "groups": {}}
    out["complete"] = run("SELECT COUNT(*) AS n FROM responses WHERE status = 'complete'")[0]["n"]
    for name, group in quotas["groups"].items():
        counts = {
            r["cell"]: r["n"]
            for r in run(
                "SELECT json_extract(quota_cells, ?) AS cell, COUNT(*) AS n "
                "FROM responses WHERE status = 'complete' GROUP BY cell",
                ("$." + name,),
            )
        }
        cells = []
        for c in group["cells"]:
            done = counts.get(c["id"], 0)
            share = done / c["target"]
            cells.append({**c, "complete": done, "pct": round(share, 3), "full": done >= c["target"],
                          "warning": quotas["warn_at"] <= share < 1})
        out["groups"][name] = {"source": group["source"], "cells": cells}
    return out


@app.get("/api/quota-status")
def api_quota_status():
    return jsonify(quota_status())


@app.post("/api/responses")
def api_save_response():
    p = request.get_json(force=True, silent=True) or {}
    missing = [k for k in ("respondent_id", "survey_id", "status", "answers") if k not in p]
    if missing:
        return jsonify(error="missing fields: " + ", ".join(missing)), 400
    if p["status"] not in STATUSES:
        return jsonify(error="status must be one of " + ", ".join(sorted(STATUSES))), 400
    cells = {name: cell_for(g, p["answers"]) for name, g in load_quotas()["groups"].items()}
    try:
        run(
            "INSERT INTO responses (respondent_id, survey_id, version, status, terminate_reason, started_at, "
            "ended_at, duration_sec, quota_cells, answers, timings_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p["respondent_id"], p["survey_id"], p.get("version"), p["status"], p.get("terminate_reason"),
             p.get("started_at"), p.get("ended_at"), p.get("duration_sec"), json.dumps(cells),
             json.dumps(p["answers"]), json.dumps(p.get("timings_ms", {}))),
            write=True,
        )
    except sqlite3.IntegrityError:
        return jsonify(error="this respondent_id has already been saved"), 409
    return jsonify(ok=True, quota=quota_status()), 201


@app.get("/")
def home():
    return redirect("/survey/")


# Only the survey and config folders are served, never the database or source code.
@app.get("/survey/")
@app.get("/survey/<path:name>")
def survey_files(name="index.html"):
    return send_from_directory(ROOT / "survey", name)


@app.get("/dashboard/")
@app.get("/dashboard/<path:name>")
def dashboard_files(name="index.html"):
    return send_from_directory(ROOT / "dashboard", name)


@app.get("/reports/data_quality_report.json")
def pipeline_report():
    return send_from_directory(ROOT / "reports", "data_quality_report.json")


@app.get("/config/<path:name>")
def config_files(name):
    return send_from_directory(CONFIG_DIR, name)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        DB_PATH.unlink(missing_ok=True)
        print("Database cleared.")
    else:
        print("Survey:    http://localhost:8000/survey/")
        print("Dashboard: http://localhost:8000/dashboard/")
        app.run(host="127.0.0.1", port=int(os.environ.get("SURVEYOPS_PORT", 8000)))