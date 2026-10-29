"""ubuntu-latest-check: find GitHub Actions jobs that will break when
ubuntu-latest moves to a new Ubuntu release."""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, checks, data, workflow
from .workflow import Unresolved

WORKFLOW_DIR = ".github/workflows"


@dataclass
class Context:
    from_version: str
    to_version: str
    apt_from: set
    apt_to: set
    python_from: list
    python_to: list
    reader: object = None


def _gh(args, timeout=60, retries=4):
    """Run gh and return stdout. Connection errors are retried with backoff."""
    for attempt in range(retries):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout
        if "error connecting" not in proc.stderr and "timeout" not in proc.stderr.lower():
            break
        time.sleep(2 ** attempt)
    raise RuntimeError(f"gh {' '.join(args)}: {proc.stderr.strip()}")


def local_source(path):
    """Yield (display name, text) for workflow files under a path, and return
    a reader for files relative to the repository root."""
    path = Path(path)
    if path.is_file():
        files = [path]
        root = path.parent.parent.parent if path.parent.name == "workflows" else path.parent
    else:
        root = path
        wf = path / WORKFLOW_DIR
        files = sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml")) if wf.is_dir() else []

    def reader(name):
        p = root / name
        return p.read_text() if p.is_file() else None

    return [(str(f), f.read_text()) for f in files], reader


def remote_source(repo):
    """Workflow files of a GitHub repository's default branch, read with gh."""
    try:
        listing = json.loads(_gh(["api", f"repos/{repo}/contents/{WORKFLOW_DIR}"]))
    except RuntimeError as e:
        if "Not Found" in str(e) or "404" in str(e):
            return [], lambda name: None
        raise

    def fetch(name):
        try:
            item = json.loads(_gh(["api", f"repos/{repo}/contents/{name}"]))
        except RuntimeError:
            return None
        return base64.b64decode(item.get("content", "")).decode("utf-8", "replace")

    files = []
    for item in listing if isinstance(listing, list) else []:
        if item.get("type") == "file" and item["name"].endswith((".yml", ".yaml")):
            files.append((f"{repo}/{item['path']}", fetch(item["path"]) or ""))
    return files, fetch


def _line_of(text, pattern, start=0):
    lines = text.splitlines()
    rx = re.compile(pattern)
    for i in range(start, len(lines)):
        if rx.search(lines[i]):
            return i + 1
    return None


def scan_file(name, text, ctx, labels):
    """Return (findings, notes, job count) for one workflow file."""
    try:
        jobs = workflow.load(text)
    except Exception as e:  # yaml.YAMLError and friends
        return [], [f"{name}: not parsed: {e}"], 0
    findings, notes, matched_jobs = {}, [], 0
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_line = _line_of(text, rf"^\s+{re.escape(str(job_id))}\s*:") or 1
        try:
            combos = workflow.expand_matrix(job.get("strategy"))
        except Unresolved as e:
            notes.append(f"{name}: job {job_id}: matrix not expanded: {e}")
            continue
        job_matched = 0
        job_keys = []
        for combo in combos:
            try:
                runner = workflow.runs_on_labels(job, combo)
            except Unresolved as e:
                notes.append(f"{name}: job {job_id}: runs-on not resolved: {e}")
                break
            if "self-hosted" in runner or not set(runner) & labels:
                continue
            job_matched += 1
            try:
                container = workflow.container_image(job, combo)
            except Unresolved as e:
                notes.append(f"{name}: job {job_id}: container not resolved: {e}")
                continue
            if container:
                notes.append(f"{name}: job {job_id}: runs in container {container}, steps do not use the runner's Ubuntu")
                continue
            found, job_notes = checks.check_job(job, combo, ctx)
            notes += [f"{name}: job {job_id}: {n}" for n in job_notes]
            label = ", ".join(f"{k}={v}" for k, v in combo.items() if not isinstance(v, (dict, list)))
            for f in found:
                key = (job_id, f["kind"], f["item"])
                if key in findings:
                    f.pop("anchor", None)
                else:
                    job_keys.append(key)
                    item_rx = f.pop("anchor", None) or rf"(?<![\w.+-]){re.escape(f['item'])}(?![\w+-]|\.\d)"
                    findings[key] = dict(f, file=name, job=str(job_id),
                                         line=_line_of(text, item_rx, job_line - 1) or job_line, matrix=[])
                if label and label not in findings[key]["matrix"]:
                    findings[key]["matrix"].append(label)
        # Only name matrix combinations when the finding does not apply to
        # every combination of the job that runs on the checked labels.
        for key in job_keys:
            if len(findings[key]["matrix"]) == job_matched:
                findings[key]["matrix"] = []
        matched_jobs += bool(job_matched)
    return list(findings.values()), list(dict.fromkeys(notes)), matched_jobs


def build_context(from_version, to_version, refresh):
    py = data.python_versions(refresh)
    return Context(
        from_version=from_version,
        to_version=to_version,
        apt_from=data.ubuntu_packages(from_version, refresh),
        apt_to=data.ubuntu_packages(to_version, refresh),
        python_from=py.get(from_version, []),
        python_to=py.get(to_version, []),
    )


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ubuntu-latest-check",
        description="Find GitHub Actions jobs on ubuntu-latest that install apt packages or Python "
                    "versions the next Ubuntu release no longer provides.",
    )
    p.add_argument("paths", nargs="*", help="repository directories or workflow files (default: .)")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                   help="scan a GitHub repository's default branch via gh (repeatable)")
    p.add_argument("--owner", metavar="OWNER", help="scan every non-archived source repository of a user or org via gh")
    p.add_argument("--label", action="append", metavar="LABEL",
                   help="runner label to check (repeatable, default: ubuntu-latest)")
    p.add_argument("--from", dest="from_version", default="24.04", help="current Ubuntu version (default: 24.04)")
    p.add_argument("--to", dest="to_version", default="26.04", help="new Ubuntu version (default: 26.04)")
    p.add_argument("--json", action="store_true", help="print findings as JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="also list what was skipped")
    p.add_argument("--refresh", action="store_true", help="re-download package indexes and the Python manifest")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    labels = set(args.label or ["ubuntu-latest"])

    sources = []
    try:
        repos = list(args.repo)
        if args.owner:
            out = _gh(["repo", "list", args.owner, "--no-archived", "--source", "--limit", "1000",
                       "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"], timeout=120)
            repos += out.split()
        for repo in repos:
            sources.append(remote_source(repo))
        for path in args.paths or ([] if repos else ["."]):
            sources.append(local_source(path))
        ctx = build_context(args.from_version, args.to_version, args.refresh)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        print(f"ubuntu-latest-check: {e}", file=sys.stderr)
        return 2

    findings, notes, jobs, files = [], [], 0, 0
    for file_list, reader in sources:
        ctx.reader = reader
        for name, text in file_list:
            f, n, j = scan_file(name, text, ctx, labels)
            findings += f
            notes += n
            jobs += j
            files += 1

    errors = sum(1 for f in findings if f["severity"] == "error")
    if args.json:
        print(json.dumps({"from": args.from_version, "to": args.to_version, "labels": sorted(labels),
                          "files": files, "jobs": jobs, "findings": findings, "skipped": notes}, indent=2))
    else:
        for f in findings:
            where = f"{f['file']}:{f['line']}"
            matrix = f" [{'; '.join(f['matrix'])}]" if f["matrix"] else ""
            print(f"{where}: {f['severity']}: job {f['job']}{matrix}: {f['kind']} {f['item']}: {f['detail']}")
        if args.verbose:
            for n in notes:
                print(f"skipped: {n}")
        print(f"{files} workflow files, {jobs} jobs on {', '.join(sorted(labels))}, "
              f"{errors} errors, {len(findings) - errors} warnings "
              f"(Ubuntu {args.from_version} -> {args.to_version})"
              + (f", {len(notes)} items skipped (-v)" if notes and not args.verbose else ""))
    return 1 if errors else 0
