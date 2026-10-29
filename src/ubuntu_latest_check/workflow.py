"""Read GitHub Actions workflow files and expand each job into the concrete
matrix combinations it runs as."""

import itertools
import re

import yaml

EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
MATRIX_REF = re.compile(r"^matrix\.([A-Za-z0-9_-]+)((?:\.[A-Za-z0-9_-]+|\[\d+\])*)$")
LITERAL = re.compile(r"^'((?:[^']|'')*)'$")


class Unresolved(Exception):
    """A value depends on an expression this tool does not evaluate."""


def load(text):
    """Parse a workflow file. Returns the mapping of job id -> job, or {}."""
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return {}
    jobs = doc.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _matches(combo, entry):
    return all(k in combo and combo[k] == v for k, v in entry.items())


def expand_matrix(strategy):
    """Return the list of matrix combinations (dicts) for a job's strategy,
    following GitHub's include/exclude rules. A job without a matrix yields
    one empty combination. Raises Unresolved for a matrix built from an
    expression."""
    if not isinstance(strategy, dict) or "matrix" not in strategy:
        return [{}]
    matrix = strategy["matrix"]
    if not isinstance(matrix, dict):
        raise Unresolved("strategy.matrix is an expression")
    axes = {}
    for key, values in matrix.items():
        if key in ("include", "exclude"):
            continue
        if not isinstance(values, list):
            raise Unresolved(f"matrix.{key} is an expression")
        axes[key] = values
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list):
        raise Unresolved("matrix include/exclude is an expression")

    combos = [dict(zip(axes, vals)) for vals in itertools.product(*axes.values())] if axes else []
    combos = [c for c in combos if not any(isinstance(e, dict) and _matches(c, e) for e in exclude)]
    originals = [dict(c) for c in combos]

    for entry in include:
        if not isinstance(entry, dict):
            raise Unresolved("matrix include entry is an expression")
        added = False
        for combo, original in zip(combos, originals):
            # An include entry extends every original combination whose
            # original values it does not overwrite. Combinations created by
            # an earlier include entry (original is None) are not extended.
            if original is not None and all(original.get(k, v) == v for k, v in entry.items() if k in axes):
                combo.update(entry)
                added = True
        if not added:
            combos.append(dict(entry))
            originals.append(None)
    return combos or [{}]


def _operand(combo, ref):
    """Value of one operand: a 'string literal' or a matrix reference such as
    matrix.os, matrix.build.image or matrix.os_python[0]. Returns None when
    the matrix has no such value."""
    lit = LITERAL.match(ref)
    if lit:
        return lit.group(1).replace("''", "'")
    m = MATRIX_REF.match(ref)
    if not m:
        raise Unresolved(f"${{{{ {ref} }}}}")
    value = combo.get(m.group(1))
    for part in re.findall(r"\.([A-Za-z0-9_-]+)|\[(\d+)\]", m.group(2)):
        if part[0] and isinstance(value, dict):
            value = value.get(part[0])
        elif part[1] and isinstance(value, list):
            value = value[int(part[1])] if int(part[1]) < len(value) else None
        else:
            value = None
    return value


def _lookup(combo, ref):
    """Evaluate an expression made of matrix references and string literals
    joined by ||, such as matrix.os || 'ubuntu-latest'."""
    operands = [o.strip() for o in ref.split("||")]
    value = None
    for operand in operands:
        value = _operand(combo, operand)
        if value not in (None, "", False, 0):
            return value
    if value is None:
        raise Unresolved(f"${{{{ {ref} }}}} is not set in this combination")
    return value


def substitute(value, combo):
    """Replace ${{ matrix.* }} references in a string. A string that is a
    single expression keeps the matrix value's type (so lists survive).
    Raises Unresolved for any other expression."""
    if not isinstance(value, str):
        return value
    whole = EXPR.fullmatch(value.strip())
    if whole:
        return _lookup(combo, whole.group(1))
    return EXPR.sub(lambda m: str(_lookup(combo, m.group(1))), value)


def substitute_lenient(text, combo):
    """Like substitute, but leaves unresolvable expressions in place. Used for
    shell scripts, where an unrelated ${{ secrets.X }} must not hide an
    apt-get line."""

    def repl(m):
        try:
            return str(_lookup(combo, m.group(1)))
        except Unresolved:
            return m.group(0)

    return EXPR.sub(repl, text) if isinstance(text, str) else text


def runs_on_labels(job, combo):
    """Return the runner labels of one job combination as a list of
    strings. Raises Unresolved when they depend on an expression."""
    runs_on = substitute(job.get("runs-on"), combo)
    if isinstance(runs_on, str):
        if EXPR.search(runs_on):
            raise Unresolved(runs_on)
        return [runs_on]
    if isinstance(runs_on, list):
        out = []
        for item in runs_on:
            item = substitute(item, combo)
            if not isinstance(item, str):
                raise Unresolved("runs-on list item")
            out.append(item)
        return out
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            return [substitute(labels, combo)]
        if isinstance(labels, list):
            return [substitute(x, combo) for x in labels]
        return []
    if runs_on is None and "uses" in job:
        return []  # calls a reusable workflow; that file is checked on its own
    raise Unresolved("runs-on missing or not understood")


def container_image(job, combo):
    """The container image a job combination runs its steps in, or None.
    A matrix reference that is unset in this combination means no
    container, as on GitHub."""
    container = job.get("container")
    if isinstance(container, dict):
        container = container.get("image")
    if not container:
        return None
    try:
        image = substitute(container, combo)
    except Unresolved as e:
        if "is not set in this combination" in str(e):
            return None
        raise
    if isinstance(image, dict):
        image = image.get("image")
    return image or None
