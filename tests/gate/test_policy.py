import re
import unittest
from pathlib import Path

from gate import miniyaml
from gate.policy import AUTO_ALLOWED, FLOOR_CLASSES, SCHEMA, PolicyError, parse
from tests.gate.helpers import POLICY

RECORD = Path(__file__).resolve().parents[2] / "docs" / "design" / "2026-11-10-agent-kit.md"


def with_line(old, new):
    assert old in POLICY, old
    return POLICY.replace(old, new)


class PolicyTest(unittest.TestCase):
    def test_record_example_is_valid(self):
        example = RECORD.read_text().split("### C1 Policy file v1\n```yaml\n")[1].split("```")[0]
        example = example.replace('required_checks: ["ci / test"]', "required_checks: [{name: test, app: github-actions}]")  # Drift 6-7
        policy = parse(example.replace("<40-hex>", "a" * 40))
        self.assertEqual(policy["classes"]["billing-ui"], {"paths": ["src/billing/**"], "floor": "payments"})
        self.assertEqual(policy["journeys"][0]["data_assertions"][0]["expect"], [{"name": "Ada"}])

    def test_floor_class_cannot_be_put_on_auto_list(self):
        for floor in ("migrations", "auth", "payments", "tenancy", "destructive", "governance", "review"):
            with self.subTest(floor), self.assertRaises(PolicyError):
                parse(with_line("auto_merge: [docs, kit-bump]", f"auto_merge: [docs, {floor}]"))

    def test_schema_floor_lists_match_the_kit_floor(self):
        self.assertEqual(SCHEMA["properties"]["floor_extra"]["propertyNames"]["enum"], list(FLOOR_CLASSES))
        self.assertEqual(SCHEMA["properties"]["classes"]["additionalProperties"]["properties"]["floor"]["enum"], list(FLOOR_CLASSES))
        self.assertEqual(SCHEMA["properties"]["auto_merge"]["items"]["enum"], list(AUTO_ALLOWED))

    def test_repo_class_cannot_reuse_a_kit_or_floor_name(self):
        for name in ("docs", "kit-bump", "review", "auth"):
            with self.subTest(name), self.assertRaises(PolicyError):
                parse(POLICY + f'classes:\n  {name}: {{paths: ["src/**"]}}\n')

    def test_no_key_removes_floor_paths(self):
        for text in (POLICY + "floor_remove: [governance]\n",
                     with_line("  governance:", "  everything:"),
                     with_line('  migrations: {paths: ["db/migrations/**"]}', "  migrations: {}")):
            with self.subTest(text[-40:]), self.assertRaises(PolicyError):
                parse(text)

    def test_caps_can_lower_but_not_raise(self):
        parse(POLICY + "caps: {runs_per_repo: 0, daily_usd: 5, max_attempts: 1}\n")
        for caps in ("{runs_per_repo: 2}", "{max_attempts: 3}", "{daily_usd: -1}"):
            with self.subTest(caps), self.assertRaises(PolicyError):
                parse(POLICY + f"caps: {caps}\n")

    def test_required_checks_name_their_app(self):
        for checks in ("[test]", "[{name: test}]", "[{name: test, app: Bad_App}]"):
            with self.subTest(checks), self.assertRaises(PolicyError):
                parse(with_line("required_checks: [{name: test, app: github-actions}]", f"required_checks: {checks}"))

    def test_shape_errors_fail(self):
        for text in (with_line("version: 1", "version: true"),
                     with_line(f"kit_ref: ", "kit_ref: main  #"),
                     POLICY + 'classes:\n  x: {added_lines: ["("]}\n',
                     POLICY.replace("    data_assertions:\n      - {id: name-persisted, sql: \"select name from profiles where id = 'seed-user-1'\", expect: [{name: Ada}]}\n", "    data_assertions: []\n"),
                     ""):
            with self.subTest(text[:30]), self.assertRaises(PolicyError):
                parse(text)


class MiniYamlTest(unittest.TestCase):
    def test_scalars_and_flow(self):
        self.assertEqual(
            miniyaml.loads("a: 'it''s'  # c\nb: \"x#y\"\nc: [1, 2.5, true, null, '*.md', {k: v}]\nd:\n- x\n- y: 1\n  z: []\n"),
            {"a": "it's", "b": "x#y", "c": [1, 2.5, True, None, "*.md", {"k": "v"}], "d": ["x", {"y": 1, "z": []}]},
        )

    def test_rejects_unsupported_yaml(self):
        for text in ("a: &x 1\nb: *x\n", "a: |\n  text\n", "a: !tag x\n", "a: 1\na: 2\n", "a:\n\tb: 1\n", "a: [1,\n  2]\n", "a: 1\n---\nb: 2\n"):
            with self.subTest(text), self.assertRaises(miniyaml.YamlError):
                miniyaml.loads(text)


if __name__ == "__main__":
    unittest.main()
