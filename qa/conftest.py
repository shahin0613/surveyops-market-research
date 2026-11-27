"""Test fixtures: starts the real Flask server on a throwaway database and opens a real browser."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Server:
    def __init__(self, base, proc, db):
        self.base, self.proc, self.db = base, proc, db

    def rows(self, status):
        try:
            con = sqlite3.connect(self.db)
            n = con.execute("SELECT COUNT(*) FROM responses WHERE status = ?", (status,)).fetchone()[0]
            con.close()
            return n
        except sqlite3.OperationalError:
            return 0

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        


def start_server(workdir, config_dir=None):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "SURVEYOPS_DB": str(Path(workdir) / "test.db"), "SURVEYOPS_PORT": str(port)}
    if config_dir:
        env["SURVEYOPS_CONFIG"] = str(config_dir)
    proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "app.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/quota-status", timeout=1).read()
            return Server(base, proc, Path(workdir) / "test.db")
        except OSError:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("test server did not start")


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    s = start_server(tmp_path_factory.mktemp("shared"))
    yield s
    s.stop()


@pytest.fixture
def fresh_server(tmp_path):
    s = start_server(tmp_path)
    yield s
    s.stop()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()

def pytest_html_report_title(report):
    report.title = "SurveyOps QA Report"