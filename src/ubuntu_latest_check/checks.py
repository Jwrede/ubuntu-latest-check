"""Find what a job installs (apt packages, setup-python versions) and compare
it with what the target Ubuntu release provides."""

import json
import posixpath
import re
import shlex

from . import workflow
from .workflow import Unresolved

APT_PROGRAMS = {"apt-get", "apt"}
# apt-get options that take a separate value.
APT_OPTS_WITH_VALUE = {"-o", "-c", "-t", "--option", "--config-file", "--target-release", "--default-release"}
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&", ">", ">>", "<", "2>", "2>&1"}
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
ADDS_APT_SOURCE = re.compile(
    r"add-apt-repository|sources\.list|apt-key\b|signed-by=|\bdeb\s+(\[|https?://)|extrepo\b"
)
SETUP_PYTHON = re.compile(r"^actions/setup-python@", re.I)
APT_ACTION = re.compile(r"^awalsh128/cache-apt-pkgs-action@", re.I)
PLAYWRIGHT_INSTALL = re.compile(r"\bplaywright(?:@([\w.^~-]+))?\s+install(?:-deps)?\b")
# First Playwright release with browser builds for an Ubuntu version
# (ubuntu26.04 entries in packages/playwright-core/src/server/registry).
PLAYWRIGHT_MIN = {"26.04": (1, 61, 0)}
PYTHON_SPEC = re.compile(r"^(\d+)(?:\.(\d+|x))?(?:\.(\d+|x))?(t)?(?:-dev)?$")


def _commands(script):
    """Split a shell script into simple commands (lists of words). Words that
    shlex cannot parse end the current line."""
    script = re.sub(r"\\\r?\n", " ", script)
    for line in script.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        current = []
        try:
            for tok in lexer:
                if tok in SHELL_OPERATORS or set(tok) <= set("&|;()<>"):
                    if current:
                        yield current
                    current = []
                else:
                    current.append(tok)
        except ValueError:
            pass
        if current:
            yield current


def _normalize_package(tok):
    """Return the package name an apt-get install argument names, or None if
    the argument is not a plain archive package (a .deb path, a glob, a
    shell variable, a package to remove, a release-qualified name)."""
    if any(c in tok for c in "$*?[]{}`\\/") or tok.endswith(".deb") or tok.endswith("-"):
        return None
    tok = tok.rstrip("+")
    tok = tok.split("=", 1)[0].split(":", 1)[0]
    return tok if PACKAGE_NAME.match(tok) else None


def apt_packages(script):
    """Return the archive package names that apt-get/apt install commands in
    a shell script install, in order of appearance."""
    found = []
    # Expressions left after matrix substitution become a shell variable,
    # which _normalize_package skips.
    script = workflow.EXPR.sub("$EXPR", script)
    for words in _commands(script):
        i = 0
        while i < len(words) and (words[i] in ("sudo", "env", "nice", "time", "command") or
                                  re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or
                                  (words[i].startswith("-") and i > 0 and words[i - 1] == "sudo")):
            i += 1
        if i >= len(words) or words[i].rsplit("/", 1)[-1] not in APT_PROGRAMS:
            continue
        i += 1
        subcommand = None
        release_pinned = False
        args = []
        while i < len(words):
            w = words[i]
            if w in APT_OPTS_WITH_VALUE:
                release_pinned |= w in ("-t", "--target-release", "--default-release")
                i += 2
                continue
            if w.startswith("-"):
                release_pinned |= w.startswith(("--target-release=", "--default-release="))
                i += 1
                continue
            if subcommand is None:
                subcommand = w
            else:
                args.append(w)
            i += 1
        if subcommand != "install" or release_pinned:
            continue
        for a in args:
            name = _normalize_package(a)
            if name and name not in found:
                found.append(name)
    return found


def _python_versions_in(value):
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in re.split(r"[\n,]", str(value)) if v.strip()]


def python_available(spec, versions):
    """Whether a setup-python version spec matches one of the given full
    versions. Returns None for specs this tool does not interpret (ranges,
    PyPy, GraalPy)."""
    m = PYTHON_SPEC.match(spec)
    if not m:
        return None
    freethreaded = bool(m.group(4))
    versions = [v[:-1] if freethreaded else v for v in versions if v.endswith("t") == freethreaded]
    parts = [p for p in m.groups()[:3] if p is not None]
    prefix = []
    for p in parts:
        if p == "x":
            break
        prefix.append(p)
    for v in versions:
        nums = re.split(r"[.-]", v)
        if nums[: len(prefix)] == prefix:
            return True
    return False


def _read_version_file(reader, name):
    text = reader(name)
    if text is None:
        raise Unresolved(f"python-version-file {name} not found")
    if name.endswith(".tool-versions"):
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "python":
                return parts[1:]
        raise Unresolved(f"no python entry in {name}")
    if name.endswith(".python-version"):
        return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")][:1]
    raise Unresolved(f"python-version-file {name} is not .python-version or .tool-versions")


def _version_tuple(text):
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def playwright_version(reader, directory):
    """Return (version, source file) of the Playwright that a Node project
    in directory has locked, or (None, None)."""
    directory = posixpath.normpath(directory or ".").strip("/")
    prefix = "" if directory == "." else f"{directory}/"
    text = reader(prefix + "package-lock.json")
    if text:
        try:
            lock = json.loads(text)
        except ValueError:
            lock = {}
        packages = lock.get("packages") or {}
        deps = lock.get("dependencies") or {}
        for name in ("playwright-core", "playwright", "@playwright/test"):
            entry = packages.get(f"node_modules/{name}") or deps.get(name)
            if isinstance(entry, dict) and entry.get("version"):
                return entry["version"], prefix + "package-lock.json"
    text = reader(prefix + "pnpm-lock.yaml")
    if text:
        m = re.search(r"^\s+'?/?playwright-core@(\d+\.\d+\.\d+)", text, re.M)
        if m:
            return m.group(1), prefix + "pnpm-lock.yaml"
    text = reader(prefix + "yarn.lock")
    if text:
        m = re.search(r'^"?playwright-core@[^\n]*\n\s+version:?\s+"?(\d+\.\d+\.\d+)', text, re.M)
        if m:
            return m.group(1), prefix + "yarn.lock"
    return None, None


def check_playwright(job, combo, ctx, notes):
    minimum = PLAYWRIGHT_MIN.get(ctx.to_version)
    if not minimum:
        return []
    defaults = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
    seen = set()
    findings = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("run"), str):
            continue
        m = PLAYWRIGHT_INSTALL.search(step["run"])
        if not m:
            continue
        if m.group(1):
            version, source = m.group(1), "the install command"
        elif ctx.reader is None:
            continue
        else:
            directory = step.get("working-directory") or defaults or ""
            cds = re.findall(r"(?:^|[;&|\n]\s*)cd\s+([^\s;&|]+)", step["run"][: m.start()])
            if cds and not cds[-1].startswith(("/", "~", "$", "-")):
                directory = f"{directory}/{cds[-1]}" if directory else cds[-1]
            directory = workflow.substitute_lenient(directory, combo)
            version, source = playwright_version(ctx.reader, directory)
            if version is None and directory:
                version, source = playwright_version(ctx.reader, "")
        if version is None:
            notes.append("playwright install: no package-lock.json, pnpm-lock.yaml or yarn.lock with Playwright found")
            continue
        parsed = _version_tuple(version.lstrip("^~"))
        if parsed is None or version in seen:
            continue
        seen.add(version)
        if parsed < minimum:
            findings.append({
                "kind": "playwright",
                "item": version,
                "severity": "error",
                "detail": f"Playwright {version} (from {source}) does not support Ubuntu {ctx.to_version}, "
                          f"'playwright install' fails there; {'.'.join(map(str, minimum))} is the first release "
                          f"that does",
                "anchor": r"playwright(@\S+)?\s+install",
            })
    return findings


def check_job(job, combo, ctx):
    """Check one job combination. Returns (findings, notes): findings are
    dicts with kind, item, severity, detail; notes are strings for things
    that were skipped."""
    findings, notes = [], []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return findings, notes
    script_text = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str):
            script_text.append(workflow.substitute_lenient(run, combo))
    adds_source = any(ADDS_APT_SOURCE.search(s) for s in script_text)

    packages = []
    for script in script_text:
        packages += [p for p in apt_packages(script) if p not in packages]

    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if not isinstance(uses, str):
            continue
        with_ = step.get("with") or {}
        if APT_ACTION.match(uses):
            try:
                value = workflow.substitute(with_.get("packages", ""), combo)
            except Unresolved as e:
                notes.append(f"{uses} packages: {e}")
                continue
            for tok in re.split(r"[\s,]+", str(value or "")):
                name = _normalize_package(tok) if tok else None
                if name and name not in packages:
                    packages.append(name)
        elif SETUP_PYTHON.match(uses):
            try:
                specs = _python_versions_in(workflow.substitute(with_.get("python-version"), combo))
                if not specs and with_.get("python-version-file"):
                    specs = _read_version_file(ctx.reader, workflow.substitute(with_["python-version-file"], combo))
            except Unresolved as e:
                notes.append(f"setup-python version: {e}")
                continue
            for spec in specs:
                before = python_available(spec, ctx.python_from)
                after = python_available(spec, ctx.python_to)
                if before is None:
                    notes.append(f"setup-python version {spec!r} not interpreted")
                elif before and not after:
                    findings.append({
                        "kind": "python",
                        "item": spec,
                        "severity": "error",
                        "detail": f"actions/setup-python has no Python {spec} build for Ubuntu {ctx.to_version} "
                                  f"(it has one for {ctx.from_version})",
                    })

    findings += check_playwright(job, combo, ctx, notes)

    for name in packages:
        if name in ctx.apt_to or name not in ctx.apt_from:
            continue
        detail = f"apt package exists in Ubuntu {ctx.from_version} but not in {ctx.to_version}"
        severity = "error"
        if adds_source:
            detail += " (the job adds an apt source, which may provide it)"
            severity = "warning"
        findings.append({"kind": "apt", "item": name, "severity": severity, "detail": detail})
    return findings, notes
