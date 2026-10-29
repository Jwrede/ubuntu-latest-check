# ubuntu-latest-check

GitHub is moving the `ubuntu-latest` runner label from Ubuntu 24.04 to Ubuntu 26.04 between
19 October and 19 November 2026. `ubuntu-latest-check` reads your workflow files and lists the
jobs on `ubuntu-latest` that will fail on 26.04 for one of three reasons that can be checked
before the job runs:

1. **apt packages that 26.04 no longer has.** Every package named in `apt-get install` /
   `apt install` (in `run:` steps) or in `awalsh128/cache-apt-pkgs-action` is looked up in the
   Ubuntu archive indexes of both releases (release, `-updates` and `-security` pockets, all
   four components, including `Provides`). A package that exists in 24.04 but not in 26.04 is
   reported. Examples: `gcc-9`, `gcc-10`, `clang-14` to `clang-16`, `postgresql-client-16`,
   `python3.12-venv`, `libicu74`, `lzma-dev`, `libatlas-base-dev`.
2. **Python versions that `actions/setup-python` cannot install on 26.04.** The requested
   versions (literal, matrix, multi-line, `python-version-file` with `.python-version` or
   `.tool-versions`) are compared with the
   [actions/python-versions manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json).
   On 26.04 there are no builds for 3.8 and 3.9, and only for patch releases built after
   26.04 support was added (3.12.13 and later, for example), so exact pins such as `3.12.3`
   also fail.
3. **Playwright versions without Ubuntu 26.04 support.** For jobs that run
   `playwright install` or `playwright install-deps`, the locked Playwright version is read
   from `package-lock.json`, `pnpm-lock.yaml` or `yarn.lock` (in the step's
   `working-directory` or a preceding `cd`). Versions before 1.61.0 stop with
   `Playwright does not support chromium on ubuntu26.04-x64`; 1.61.0 is the first release
   with `ubuntu26.04` entries in its browser registry.

Matrices are expanded with GitHub's `include`/`exclude` rules, so a finding names the
combinations it applies to. Jobs that run in a `container:` or on `self-hosted` runners are
skipped, because their steps do not use the hosted Ubuntu image.

## Why

- [actions/runner-images#14748](https://github.com/actions/runner-images/issues/14748),
  the announcement of the switch: 241 reactions and 90 comments. Its software table lists
  Java 17 as the default on both images, while the image definition
  ([toolset-2604.json](https://github.com/actions/runner-images/blob/main/images/ubuntu/toolsets/toolset-2604.json))
  sets 25. The mitigation offered is to pin `ubuntu-24.04` or test against `ubuntu-26.04`.
- Since the rollout began, projects report the same failures one by one, for example:
  Python 3.8/3.9 in setup-python
  ([BOSWatch/BW3-Core#163](https://github.com/BOSWatch/BW3-Core/issues/163),
  [konflux-ci/diffused#56](https://github.com/konflux-ci/diffused/issues/56),
  [VibiumDev/vibium#685](https://github.com/VibiumDev/vibium/issues/685),
  [Stephenson-Software/graphik#60](https://github.com/Stephenson-Software/graphik/issues/60)),
  Playwright
  ([DataBiosphere/data-browser#4996](https://github.com/DataBiosphere/data-browser/issues/4996),
  [cncf/maintainer-d#179](https://github.com/cncf/maintainer-d/issues/179),
  [CieloVistaSoftware/wb-starter#1955](https://github.com/CieloVistaSoftware/wb-starter/issues/1955)),
  and old compilers from apt
  ([graph-algorithms/edge-addition-planarity-suite#377](https://github.com/graph-algorithms/edge-addition-planarity-suite/issues/377)).
  Each of these was found after CI went red.
- The previous switch to 24.04 had the same pattern:
  [actions/runner-images#10636](https://github.com/actions/runner-images/issues/10636)
  (384 reactions, 168 comments) and
  [tauri-apps/tauri#9662](https://github.com/tauri-apps/tauri/issues/9662), "libwebkit2gtk-4.0
  not available in Ubuntu 24" (150 reactions, 93 comments).
- Existing tools do not cover this. actionlint checks that runner labels are valid;
  [lastgreen](https://github.com/thanatchon36/lastgreen) compares a failed run with the last
  green one, which helps after a failure. I found no tool that checks workflows against the
  next image before the switch.

## Installation

Python 3.9 or newer.

```sh
pipx install git+https://github.com/Jwrede/ubuntu-latest-check
# or
uv tool install git+https://github.com/Jwrede/ubuntu-latest-check
```

Scanning remote repositories (`--repo`, `--owner`) uses an authenticated
[GitHub CLI](https://cli.github.com/) (`gh`).

## Usage

```sh
ubuntu-latest-check                       # workflows of the repository in the current directory
ubuntu-latest-check path/to/repo path/to/workflow.yml
ubuntu-latest-check --repo owner/name     # default branch, read through gh
ubuntu-latest-check --owner my-org        # every non-archived source repository
ubuntu-latest-check --label ubuntu-24.04  # also check jobs you pinned, before unpinning them
ubuntu-latest-check --json                # machine-readable report
ubuntu-latest-check -v                    # also list what was skipped and why
```

The exit code is 1 if there is at least one error, 0 otherwise, and 2 if the input could not be
read, so the command can run as a CI step. The first run downloads the package indexes of both
releases (about 47 MB) and caches the package names in `~/.cache/ubuntu-latest-check` for a day
(`--refresh` downloads again). `--from` and `--to` select other releases (20.04, 22.04, 24.04,
26.04).

## Example

The workflow in [`examples/demo`](examples/demo/.github/workflows/ci.yml) runs a Python matrix
on Linux and Windows, installs a few apt packages and runs Playwright tests from `web/`:

```text
$ ubuntu-latest-check examples/demo
examples/demo/.github/workflows/ci.yml:9: error: job test [os=ubuntu-latest, python-version=3.9]: python 3.9: actions/setup-python has no Python 3.9 build for Ubuntu 26.04 (it has one for 24.04)
examples/demo/.github/workflows/ci.yml:23: error: job test: apt gcc-10: apt package exists in Ubuntu 24.04 but not in 26.04
examples/demo/.github/workflows/ci.yml:23: error: job test: apt postgresql-client-16: apt package exists in Ubuntu 24.04 but not in 26.04
examples/demo/.github/workflows/ci.yml:34: error: job e2e: playwright 1.59.1: Playwright 1.59.1 (from web/package-lock.json) does not support Ubuntu 26.04, 'playwright install' fails there; 1.61.0 is the first release that does
examples/demo/.github/workflows/ci.yml:33: error: job e2e: apt libgdk-pixbuf2.0-0: apt package exists in Ubuntu 24.04 but not in 26.04
examples/demo/.github/workflows/ci.yml:33: error: job e2e: apt libx264-164: apt package exists in Ubuntu 24.04 but not in 26.04
1 workflow files, 2 jobs on ubuntu-latest, 6 errors, 0 warnings (Ubuntu 24.04 -> 26.04)
```

On public repositories (default branches on 29 October 2026) it reported, among others,
`lzma-dev` in [python/mypy](https://github.com/python/mypy/blob/master/.github/workflows/test.yml),
`libatlas-base-dev` in [numpy/numpy](https://github.com/numpy/numpy/blob/main/.github/workflows/linux_blas.yml),
and Playwright 1.58.2 in two `ubuntu-latest` workflows of CieloVistaSoftware/wb-starter, the
project whose issue is linked above. With `--label ubuntu-24.04` it reports the Playwright
1.57.0 lock in cncf/maintainer-d that its issue describes.

## Limits

- Only what is written in the workflow is checked. Packages installed by scripts the workflow
  calls (`./ci/install.sh`), by Makefiles, by `xargs apt-get install < file`, or through a
  shell variable are not seen.
- A missing package becomes a warning instead of an error when the job adds an apt source
  (`add-apt-repository`, `sources.list`, `signed-by=`), since that source may provide it.
  Whether a PPA builds for 26.04 is not checked. Packages that exist in neither release (from
  third-party repositories) are ignored.
- Only x64 is checked (`ubuntu-latest` is x64; arm64 images are not part of this switch).
- Expressions other than `matrix.*` references, `'literal'` strings and `||` are not
  evaluated; jobs or values that depend on them are listed with `-v` as skipped. So are
  setup-python ranges, PyPy and GraalPy, and `python-version-file: pyproject.toml`.
- Other changes in the 26.04 image are not checked: Ruby versions for `ruby/setup-ruby`, the
  default Java version, `/tmp` as tmpfs with a quota, the agent directory layout on Azure
  DevOps, or newer compiler and kernel versions breaking a build.
- Remote scans read only the default branch.
- The Playwright threshold (1.61.0) comes from Playwright's browser registry source; it only
  applies to `--to 26.04`.

## License

MIT
