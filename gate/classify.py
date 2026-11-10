"""Change class from the actual diff (C1 classes, D7). The most restrictive class wins."""

import re

from gate.policy import FLOOR_CLASSES

PRECEDENCE = FLOOR_CLASSES + ("review", "kit-bump", "docs")
GOVERNANCE_PATHS = (".agent-kit/**", ".github/**", ".cursor/**", "CODEOWNERS", "docs/CODEOWNERS", "**/AGENTS.md", "**/CLAUDE.md",
                    "**/fixtures/**", "**/evals/**")
DOCS_PATHS = ("docs/**", "**/*.md")
KIT_CALLER_PATHS = (".github/workflows/**", ".agent-kit/policy.yml")
DESTRUCTIVE_LINE = re.compile(r"\b(DROP|TRUNCATE|DELETE FROM)\b|\brm -rf\b")
KIT_BUMP_LINE = re.compile(
    r"^\s*(-\s+)?uses:\s*yudduy/agent-kit/\S+@[0-9a-f]{40}\s*(#.*)?$|^\s*kit_ref:\s*[0-9a-f]{40}\s*(#.*)?$"
)


def glob_matches(glob, path):
    pattern = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            pattern, i = pattern + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            pattern, i = pattern + ".*", i + 2
        elif glob[i] == "*":
            pattern, i = pattern + "[^/]*", i + 1
        elif glob[i] == "?":
            pattern, i = pattern + "[^/]", i + 1
        else:
            pattern, i = pattern + re.escape(glob[i]), i + 1
    return re.fullmatch(pattern, path) is not None


def classify(files, policy):
    """files: compare-API `files` entries. policy: a valid C1 dict, or None for the kit floor alone.

    Returns (class, classes_hit) with classes_hit ordered most restrictive first.
    """
    rules = _rules(policy or {})
    hits = set()
    for entry in files:
        hits |= _file_hits(entry, rules)
    ordered = sorted(hits or {"review"}, key=lambda name: (_rank(name), name))
    return ordered[0], ordered


def _rank(name):
    if name in PRECEDENCE:
        return PRECEDENCE.index(name)
    return PRECEDENCE.index("review") - 0.5


def _rules(policy):
    extra = policy.get("floor_extra", {})
    rules = []
    for name in FLOOR_CLASSES:
        paths = list(extra.get(name, {}).get("paths", []))
        lines = [re.compile(p) for p in extra.get(name, {}).get("added_lines", [])]
        if name == "governance":
            paths += GOVERNANCE_PATHS
            paths += [policy["app"]["db"]["seed"]] if "app" in policy else []
            paths += [journey["spec"] for journey in policy.get("journeys", [])]
        if name == "destructive":
            lines.append(DESTRUCTIVE_LINE)
        rules.append((name, paths, lines))
    for name, rule in policy.get("classes", {}).items():
        rules.append((rule.get("floor", name), rule.get("paths", []), [re.compile(p) for p in rule.get("added_lines", [])]))
    rules.append(("docs", DOCS_PATHS, []))
    return rules


def _file_hits(entry, rules):
    paths = [entry["filename"]] + ([entry["previous_filename"]] if entry.get("previous_filename") else [])
    patch = entry.get("patch")
    added = [line[1:] for line in (patch or "").splitlines() if line.startswith("+")]
    removed = [line[1:] for line in (patch or "").splitlines() if line.startswith("-")]
    hits = {name for name, _, line_patterns in rules if any(r.search(a) for r in line_patterns for a in added)}
    for path in paths:  # both sides of a rename; a side no class claims is review
        hits |= {name for name, globs, _ in rules if any(glob_matches(g, path) for g in globs)} or {"review"}
    if entry["status"] == "removed":
        hits.add("destructive")
    elif patch is None:
        hits.add("review")  # binary or too large: the added lines cannot be inspected
    changed = [line for line in added + removed if line.strip()]
    if changed and any(glob_matches(g, paths[0]) for g in KIT_CALLER_PATHS) and all(KIT_BUMP_LINE.match(l) for l in changed):
        hits.add("kit-bump")
    return hits
