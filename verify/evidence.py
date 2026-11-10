"""Evidence file (contract C2): assembly, schema validation and the attest-job check.

Stdlib only. The JSON Schema validator below covers the keywords that
schemas/evidence.v1.json uses, nothing more.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "evidence.v1.json"

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def _validate(value, schema, root, path, errors):
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        return _validate(value, node, root, path, errors)
    if "anyOf" in schema:
        if not any(not _collect(value, s, root, path) for s in schema["anyOf"]):
            errors.append(f"{path}: matches none of the allowed shapes")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            errors.append(f"{path}: must be of type {' or '.join(types)}")
            return
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
    if _is_type(value, "integer") and value < schema.get("minimum", value):
        errors.append(f"{path}: must be >= {schema['minimum']}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(item, schema["items"], root, f"{path}[{i}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], root, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {key}")


def _collect(value, schema, root, path):
    errors = []
    _validate(value, schema, root, path, errors)
    return errors


def schema_errors(doc):
    schema = json.loads(SCHEMA_PATH.read_text())
    return _collect(doc, schema, schema, "$")


def scenario_result(ui_result, data_assertions):
    """C2: pass only if the UI step passes and it has at least one data assertion, all passing."""
    ok = ui_result == "pass" and bool(data_assertions) and all(a["pass"] for a in data_assertions)
    return "pass" if ok else "fail"


def overall_result(scenarios):
    """Fails closed on zero scenarios. Regression entries never decide the result (C2)."""
    return "pass" if scenarios and all(s["result"] == "pass" for s in scenarios) else "fail"


def regression_entry(spec, passed_on_base, passed_on_head):
    return {"id": Path(spec).name.removesuffix(".spec.ts"), "spec": spec, "trusted": False,
            "fail_on_base": not passed_on_base, "pass_on_head": passed_on_head}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def errors(doc, artifacts_dir=None):
    """Schema errors plus the C2 rules a schema cannot express."""
    found = schema_errors(doc)
    if found:
        return found
    for s in doc["scenarios"]:
        if s["result"] != scenario_result(s["ui_result"], s["data_assertions"]):
            found.append(f"scenario {s['id']}: result disagrees with its UI and data assertions")
        if s["trace"] and s["trace"] not in {a["name"] for a in doc["artifacts"]}:
            found.append(f"scenario {s['id']}: trace {s['trace']} is not listed in artifacts")
    if doc["result"] != overall_result(doc["scenarios"]):
        found.append("result disagrees with the scenarios")
    if artifacts_dir is not None:
        root = Path(artifacts_dir).resolve()
        for a in doc["artifacts"]:
            path = (root / a["name"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                found.append(f"artifact {a['name']}: missing")
            elif sha256_file(path) != a["sha256"]:
                found.append(f"artifact {a['name']}: sha256 mismatch")
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description="Check an evidence.json before it is attested.")
    p.add_argument("evidence")
    p.add_argument("--artifacts-dir", help="directory holding the files listed in artifacts")
    for field in ("repo", "pr", "head-sha", "base-sha", "kit-ref", "run-id", "run-attempt", "workflow-ref"):
        p.add_argument(f"--{field}", help=f"expected value of {field}")
    args = p.parse_args(argv)
    doc = json.loads(Path(args.evidence).read_text())
    found = errors(doc, args.artifacts_dir)
    if not found:
        expected = {"repo": args.repo, "pr": args.pr, "head_sha": args.head_sha, "base_sha": args.base_sha,
                    "kit_ref": args.kit_ref, "run.id": args.run_id, "run.attempt": args.run_attempt,
                    "run.workflow_ref": args.workflow_ref}
        actual = {**doc, **{f"run.{k}": v for k, v in doc["run"].items()}}
        found = [f"{k}: is {actual[k]!r}, expected {v!r}" for k, v in expected.items()
                 if v is not None and str(actual[k]) != v]
    for line in found:
        print(f"evidence: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
