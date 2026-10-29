import json
from pathlib import Path

import pytest

from ubuntu_latest_check import checks, cli, workflow
from ubuntu_latest_check.workflow import Unresolved

ROOT = Path(__file__).resolve().parent.parent


def ctx(reader=None):
    return cli.Context(
        from_version="24.04",
        to_version="26.04",
        apt_from={"gcc-10", "gcc-13", "libpq-dev", "postgresql-client-16", "xvfb", "libx264-164",
                  "libgdk-pixbuf2.0-0", "libgtk-3-0", "clang-14"},
        apt_to={"gcc-13", "libpq-dev", "xvfb", "libgtk-3-0", "postgresql-client-18"},
        python_from=["3.9.25", "3.12.13", "3.13.9", "3.14.0", "3.14.0t"],
        python_to=["3.12.13", "3.13.9", "3.14.0"],
        reader=reader or (lambda name: None),
    )


@pytest.mark.parametrize("script,expected", [
    ("sudo apt-get install -y gcc-10", ["gcc-10"]),
    ("sudo apt-get update && sudo apt-get install -y --no-install-recommends a1 b2 && make", ["a1", "b2"]),
    ("sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install \\\n  x11 \\\n  libfoo-dev  # comment", ["x11", "libfoo-dev"]),
    ("sudo -E apt install pkg=1.2-3 libc6:i386 tool+", ["pkg", "libc6", "tool"]),
    ("apt-get -o Dpkg::Use-Pty=0 install -y zz1", ["zz1"]),
    ("sudo apt-get install -t noble-backports newer", []),
    ("sudo apt-get install ./local.deb $PKGS ${{ matrix.pkgs }} lib*-dev old-", []),
    ("sudo apt-get remove gcc-10; apt-cache policy gcc-10", []),
    ("sudo apt-get install -y aa1 > /dev/null 2>&1 || true", ["aa1"]),
    ("echo apt-get install not-a-command", []),
])
def test_apt_packages(script, expected):
    assert checks.apt_packages(script) == expected


def test_expand_matrix_include_exclude():
    strategy = {"matrix": {
        "os": ["ubuntu-latest", "windows-latest"],
        "py": ["3.9", "3.12"],
        "exclude": [{"os": "windows-latest", "py": "3.9"}],
        "include": [
            {"os": "ubuntu-latest", "extra": "x"},   # extends both ubuntu combos
            {"os": "macos-latest", "py": "3.13"},     # new combination
            {"os": "macos-latest", "extra": "y"},     # does not extend the include-created one
        ],
    }}
    combos = workflow.expand_matrix(strategy)
    assert {"os": "ubuntu-latest", "py": "3.9", "extra": "x"} in combos
    assert {"os": "ubuntu-latest", "py": "3.12", "extra": "x"} in combos
    assert {"os": "windows-latest", "py": "3.12"} in combos
    assert {"os": "macos-latest", "py": "3.13"} in combos
    assert {"os": "macos-latest", "extra": "y"} in combos
    assert len(combos) == 5


def test_expand_matrix_expression():
    with pytest.raises(Unresolved):
        workflow.expand_matrix({"matrix": "${{ fromJSON(needs.a.outputs.m) }}"})
    assert workflow.expand_matrix(None) == [{}]


def test_substitute():
    combo = {"os": "ubuntu-latest", "build": {"image": ""}, "pair": ["ubuntu-24.04", "3.9"], "n": 3}
    assert workflow.substitute("${{ matrix.os }}", combo) == "ubuntu-latest"
    assert workflow.substitute("${{ matrix.build.image || 'ubuntu-latest' }}", combo) == "ubuntu-latest"
    assert workflow.substitute("${{ matrix.missing || 'x' }}", combo) == "x"
    assert workflow.substitute("${{ matrix.pair[1] }}", combo) == "3.9"
    assert workflow.substitute("py${{ matrix.n }}", combo) == "py3"
    with pytest.raises(Unresolved):
        workflow.substitute("${{ inputs.os }}", combo)
    assert workflow.substitute_lenient("echo ${{ secrets.T }} ${{ matrix.os }}", combo) == \
        "echo ${{ secrets.T }} ubuntu-latest"


def test_runs_on_and_container():
    assert workflow.runs_on_labels({"runs-on": ["self-hosted", "linux"]}, {}) == ["self-hosted", "linux"]
    assert workflow.runs_on_labels({"runs-on": {"group": "g", "labels": "big"}}, {}) == ["big"]
    assert workflow.runs_on_labels({"uses": "./.github/workflows/x.yml"}, {}) == []
    job = {"container": "${{ matrix.c }}"}
    assert workflow.container_image(job, {}) is None
    assert workflow.container_image(job, {"c": "alpine:3"}) == "alpine:3"
    assert workflow.container_image({"container": {"image": "debian"}}, {}) == "debian"


@pytest.mark.parametrize("spec,versions,expected", [
    ("3.9", ["3.9.25"], True),
    ("3.9", ["3.10.1"], False),
    ("3.12.3", ["3.12.13"], False),
    ("3.12.13", ["3.12.13"], True),
    ("3.x", ["3.14.0"], True),
    ("3.14t", ["3.14.0"], False),
    ("3.14t", ["3.14.0t"], True),
    ("3.14", ["3.14.0t"], False),
    ("3.15-dev", ["3.15.0-alpha.1"], True),
    ("pypy3.10", ["3.10.1"], None),
    (">=3.9", ["3.10.1"], None),
])
def test_python_available(spec, versions, expected):
    assert checks.python_available(spec, versions) is expected


def test_playwright_version_lockfiles():
    files = {
        "web/package-lock.json": json.dumps({"packages": {"node_modules/playwright-core": {"version": "1.59.1"}}}),
        "pnpm-lock.yaml": "packages:\n\n  playwright-core@1.60.0:\n    resolution: {}\n",
        "y/yarn.lock": 'playwright-core@1.58.2:\n  version "1.58.2"\n',
    }
    reader = files.get
    assert checks.playwright_version(reader, "web") == ("1.59.1", "web/package-lock.json")
    assert checks.playwright_version(reader, "") == ("1.60.0", "pnpm-lock.yaml")
    assert checks.playwright_version(reader, "./y/") == ("1.58.2", "y/yarn.lock")
    assert checks.playwright_version(reader, "none") == (None, None)


def scan(text, reader=None, labels=("ubuntu-latest",)):
    return cli.scan_file("ci.yml", text, ctx(reader), set(labels))


def test_demo_workflow():
    demo = ROOT / "examples/demo"
    files, reader = cli.local_source(demo)
    findings, notes, jobs = cli.scan_file(files[0][0], files[0][1], ctx(reader), {"ubuntu-latest"})
    got = {(f["job"], f["kind"], f["item"], f["severity"]) for f in findings}
    assert got == {
        ("test", "python", "3.9", "error"),
        ("test", "apt", "gcc-10", "error"),
        ("test", "apt", "postgresql-client-16", "error"),
        ("e2e", "apt", "libgdk-pixbuf2.0-0", "error"),
        ("e2e", "apt", "libx264-164", "error"),
        ("e2e", "playwright", "1.59.1", "error"),
    }
    assert jobs == 2  # "pinned" runs on ubuntu-24.04
    by_item = {f["item"]: f for f in findings}
    assert by_item["3.9"]["matrix"] == ["os=ubuntu-latest, python-version=3.9"]
    assert by_item["gcc-10"]["matrix"] == []  # applies to every ubuntu-latest combination
    text = files[0][1].splitlines()
    assert "gcc-10" in text[by_item["gcc-10"]["line"] - 1]
    assert "playwright install" in text[by_item["1.59.1"]["line"] - 1]


def test_pinned_label_option():
    demo = ROOT / "examples/demo"
    files, reader = cli.local_source(demo)
    findings, _, jobs = cli.scan_file(files[0][0], files[0][1], ctx(reader), {"ubuntu-24.04"})
    assert [(f["job"], f["item"]) for f in findings] == [("pinned", "clang-14")]


def test_apt_source_downgrades_to_warning():
    findings, _, _ = scan("""
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: sudo add-apt-repository -y ppa:ubuntu-toolchain-r/test
      - run: sudo apt-get install -y gcc-10 not-in-any-release
""")
    assert [(f["item"], f["severity"]) for f in findings] == [("gcc-10", "warning")]


def test_container_and_self_hosted_jobs_are_skipped():
    findings, notes, jobs = scan("""
jobs:
  in-container:
    runs-on: ubuntu-latest
    container: debian:12
    steps:
      - run: apt-get install -y gcc-10
  self:
    runs-on: [self-hosted, ubuntu-latest]
    steps:
      - run: apt-get install -y gcc-10
  caller:
    uses: ./.github/workflows/other.yml
""")
    assert findings == []
    assert jobs == 1
    assert any("runs in container debian:12" in n for n in notes)
    assert not any("caller" in n for n in notes)


def test_python_version_file_and_unresolved():
    reader = {".python-version": "3.9.25\n"}.get
    findings, notes, _ = scan("""
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version-file: .python-version
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ inputs.py }}
      - uses: actions/setup-python@v5
        with:
          python-version: |
            3.12
            3.14t
""", reader)
    assert [(f["kind"], f["item"]) for f in findings] == [("python", "3.9.25"), ("python", "3.14t")]
    assert any("inputs.py" in n for n in notes)


def test_invalid_yaml_is_reported():
    findings, notes, jobs = scan("jobs: [unclosed")
    assert findings == [] and jobs == 0 and "not parsed" in notes[0]
