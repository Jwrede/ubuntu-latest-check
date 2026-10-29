"""Download and cache the reference data: Ubuntu package indexes and the
actions/python-versions manifest."""

import json
import lzma
import os
import time
import urllib.request
from pathlib import Path

ARCHIVE = "http://archive.ubuntu.com/ubuntu"
COMPONENTS = ("main", "restricted", "universe", "multiverse")
POCKETS = ("", "-updates", "-security")
PYTHON_MANIFEST = "https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json"

# Runner image version -> Ubuntu codename used in the archive.
CODENAMES = {
    "20.04": "focal",
    "22.04": "jammy",
    "24.04": "noble",
    "26.04": "resolute",
}

MAX_AGE = 24 * 3600


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = Path(base) / "ubuntu-latest-check"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch(url, timeout=60, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ubuntu-latest-check"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except OSError as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"download failed: {url}: {last}")


def _cached(name, build, refresh=False):
    """Return the text cached under name, rebuilding it with build() when it
    is missing, older than MAX_AGE, or refresh is set."""
    path = cache_dir() / name
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return path.read_text()
    text = build()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return text


def parse_packages(text):
    """Return the package names an index makes installable: every Package
    plus every name listed in Provides."""
    names = set()
    for line in text.splitlines():
        if line.startswith("Package:"):
            names.add(line.split(":", 1)[1].strip())
        elif line.startswith("Provides:"):
            for item in line.split(":", 1)[1].split(","):
                name = item.strip().split(" ", 1)[0]
                if name:
                    names.add(name)
    return names


def ubuntu_packages(version, refresh=False):
    """Installable amd64 package names for an Ubuntu release, across the
    release, -updates and -security pockets of all four components."""
    codename = CODENAMES.get(version)
    if not codename:
        raise ValueError(f"unknown Ubuntu version {version}; known: {', '.join(CODENAMES)}")

    def build():
        names = set()
        for pocket in POCKETS:
            for comp in COMPONENTS:
                url = f"{ARCHIVE}/dists/{codename}{pocket}/{comp}/binary-amd64/Packages.xz"
                names |= parse_packages(lzma.decompress(_fetch(url)).decode("utf-8", "replace"))
        return "\n".join(sorted(names))

    return set(_cached(f"packages-{codename}.txt", build, refresh).split())


def python_versions(refresh=False):
    """Map Ubuntu version -> list of CPython versions that
    actions/setup-python can download for linux x64. Free-threaded builds
    carry a "t" suffix (3.14.0t)."""

    def build():
        manifest = json.loads(_fetch(PYTHON_MANIFEST))
        out = {}
        for entry in manifest:
            for f in entry.get("files", []):
                if f.get("platform") != "linux" or not f.get("platform_version"):
                    continue
                suffix = {"x64": "", "x64-freethreaded": "t"}.get(f.get("arch"))
                if suffix is not None:
                    out.setdefault(f["platform_version"], []).append(entry["version"] + suffix)
        return json.dumps(out)

    return json.loads(_cached("python-versions.json", build, refresh))
