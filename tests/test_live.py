"""Runs the CLI against the real Ubuntu archive and actions/python-versions
manifest. Needs network access; enabled with LIVE=1 (the CI sets it)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to use the network")


def test_demo_against_real_data():
    proc = subprocess.run(
        [sys.executable, "-m", "ubuntu_latest_check", "--json", str(ROOT / "examples/demo")],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 1, proc.stderr
    report = json.loads(proc.stdout)
    got = {(f["job"], f["kind"], f["item"]) for f in report["findings"]}
    assert got == {
        ("test", "python", "3.9"),
        ("test", "apt", "gcc-10"),
        ("test", "apt", "postgresql-client-16"),
        ("e2e", "apt", "libgdk-pixbuf2.0-0"),
        ("e2e", "apt", "libx264-164"),
        ("e2e", "playwright", "1.59.1"),
    }


def test_clean_repository_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "ubuntu_latest_check", str(ROOT)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
