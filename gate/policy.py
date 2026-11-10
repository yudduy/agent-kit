"""Load and validate `.agent-kit/policy.yml` (contract C1) against schemas/policy.v1.json."""

import json
import re
from pathlib import Path

from gate import miniyaml

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "policy.v1.json").read_text())

# Kit floor (INV-9): hard-coded here, never read from repo policy, so a repo can add paths but never remove one.
FLOOR_CLASSES = ("governance", "destructive", "migrations", "tenancy", "payments", "auth")
AUTO_ALLOWED = ("docs", "kit-bump")


class PolicyError(ValueError):
    pass


def parse(text):
    """Return the policy dict, or raise PolicyError naming the first problem."""
    try:
        policy = miniyaml.loads(text)
    except miniyaml.YamlError as error:
        raise PolicyError(f"policy.yml is not valid YAML: {error}") from None
    problems = _validate(policy, SCHEMA, "policy")
    if problems:
        raise PolicyError(f"policy.yml breaks the v1 schema: {problems[0]}")
    for name, rule in [*policy.get("classes", {}).items(), *policy.get("floor_extra", {}).items()]:
        for pattern in rule.get("added_lines", []):
            try:
                re.compile(pattern)
            except re.error as error:
                raise PolicyError(f"policy.yml class {name}: bad regex {pattern!r}: {error}") from None
    return policy


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _validate(value, schema, where):
    """Check the JSON Schema keywords that policy.v1.json uses; return a list of problems."""
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    if "type" in schema and not _TYPES[schema["type"]](value):
        return [f"{where} must be a {schema['type']}"]
    if "const" in schema and value != schema["const"]:
        return [f"{where} must be {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where} must be one of {', '.join(schema['enum'])}"]
    if "not" in schema and not _validate(value, schema["not"], where):
        return [f"{where} is not allowed"]
    if "anyOf" in schema and all(_validate(value, s, where) for s in schema["anyOf"]):
        return [f"{where} must have {' or '.join(s['required'][0] for s in schema['anyOf'])}"]
    problems = []
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{where} must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{where} {value!r} must match {schema['pattern']}")
    if _TYPES["number"](value):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            problems.append(f"{where} must be between {schema.get('minimum')} and {schema.get('maximum')}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{where} needs at least {schema['minItems']} item")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            problems.append(f"{where} has duplicate items")
        for index, item in enumerate(value):
            problems += _validate(item, schema.get("items", {}), f"{where}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{where}.{key} is required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if "propertyNames" in schema:
                problems += _validate(key, schema["propertyNames"], f"{where} key {key!r}")
            if key in properties:
                problems += _validate(item, properties[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{where}.{key} is not a known field")
            elif isinstance(schema.get("additionalProperties"), dict):
                problems += _validate(item, schema["additionalProperties"], f"{where}.{key}")
    return problems
