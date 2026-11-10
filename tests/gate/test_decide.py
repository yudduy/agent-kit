import unittest

from gate.decide import decide, record_from_summary, summary
from tests.gate.helpers import BASE, HEAD, KIT, POLICY, change, facts, review


def gate(f):
    record = decide(f)
    assert record["conclusion"] in ("success", "failure"), record  # C5: never neutral or skipped
    return record


class DecideTest(unittest.TestCase):
    def assertBlocked(self, record, reason_part):
        self.assertEqual(record["conclusion"], "failure")
        self.assertTrue(any(reason_part in r for r in record["reasons"]), record["reasons"])

    def test_docs_only_with_valid_evidence_passes(self):
        record = gate(facts())
        self.assertEqual((record["conclusion"], record["class"], record["evidence"], record["reasons"]), ("success", "docs", "ok", []))
        self.assertEqual(record["shadow"], "would_merge")
        self.assertEqual(record_from_summary(summary(record)[1]), record)

    def test_policy_edit_in_pr_is_governance_and_blocked_inv3(self):
        record = gate(facts(files=[change(".agent-kit/policy.yml", patch="@@ -1 +1 @@\n-auto_merge: [docs]\n+auto_merge: [docs, review]")]))
        self.assertEqual(record["class"], "governance")
        self.assertBlocked(record, "class governance needs a human approval")

    def test_bot_approval_does_not_count_inv4(self):
        f = facts(files=[change("src/app.py")], reviews=[review("yudduy", user_type="Bot")])
        self.assertBlocked(gate(f), "needs a human approval")

    def test_author_self_approval_does_not_count_inv4(self):
        f = facts(files=[change("src/app.py")], reviews=[review("alice")])
        f.pr["user"] = {"login": "alice", "type": "User"}
        self.assertBlocked(gate(f), "needs a human approval")

    def test_committer_approval_does_not_count_inv4(self):
        f = facts(files=[change("src/app.py")], reviews=[review("alice")],
                  commits=[{"author": {"login": "agent-bot[bot]"}, "committer": {"login": "Alice"}}])
        self.assertBlocked(gate(f), "needs a human approval")

    def test_approval_must_be_on_head_from_a_listed_human(self):
        for reviews in ([review("alice", sha="0" * 40)], [review("mallory")],
                        [review("alice"), review("alice", state="CHANGES_REQUESTED", at="2026-11-10T11:00:00Z")]):
            with self.subTest(reviews):
                self.assertBlocked(gate(facts(files=[change("src/app.py")], reviews=reviews)), "needs a human approval")

    def test_review_from_deleted_account_is_ignored(self):
        ghost = {"user": None, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-11-10T09:00:00Z"}
        self.assertEqual(gate(facts(files=[change("src/app.py")], reviews=[ghost, review("alice")]))["conclusion"], "success")

    def test_listed_human_approval_on_head_passes_non_auto_class(self):
        record = gate(facts(files=[change("db/migrations/001.sql")], reviews=[review("alice"), review("bob")]))
        self.assertEqual((record["conclusion"], record["class"], record["approval"]), ("success", "migrations", {"by": "alice", "sha": HEAD}))

    def test_migration_never_auto_merges(self):
        record = gate(facts(files=[change("db/migrations/001.sql")]))
        self.assertEqual(record["class"], "migrations")
        self.assertBlocked(record, "class migrations needs a human approval")
        policy = POLICY.replace("auto_merge: [docs, kit-bump]", "auto_merge: [docs, migrations]")
        self.assertBlocked(gate(facts(files=[change("db/migrations/001.sql")], policy_text=policy)), "breaks the v1 schema")

    def test_hand_edited_evidence_fails_inv6(self):
        def flip(doc):
            doc["scenarios"][0]["result"] = "pass"
            doc["head_sha"] = HEAD
            doc["env"]["seed_hash"] = "sha256:edited"
        record = gate(facts(tamper=flip))
        self.assertEqual(record["evidence"], "mismatch")
        self.assertBlocked(record, "attestation does not match")

    def test_evidence_must_come_from_kit_verify_run_on_this_commit_inv6(self):
        cases = {
            "no verified attestation": dict(attestation=None),
            "signer_digest": dict(attestation={**facts().attestation, "signer_digest": "9" * 40}),
            "signer_workflow": dict(attestation={**facts().attestation, "signer_workflow": "evil/repo/.github/workflows/verify.yml"}),
            "run id": dict(attestation={**facts().attestation, "run_invocation_uri": "https://github.com/yudduy/demo/actions/runs/8/attempts/1"}),
            "other repo run": dict(attestation={**facts().attestation, "run_invocation_uri": "https://github.com/evil/demo/actions/runs/7/attempts/1"}),
        }
        for name, override in cases.items():
            with self.subTest(name):
                self.assertEqual(gate(facts(**override))["evidence"], "mismatch")
        for field, value in (("head_sha", "0" * 40), ("base_sha", "1" * 40), ("kit_ref", "2" * 40), ("policy_sha", "3" * 40),
                             ("journeys_sha", "4" * 40), ("repo", "x/y"), ("pr", 13)):
            with self.subTest(field):
                record = gate(facts(edit=lambda doc: doc.__setitem__(field, value)))
                self.assertBlocked(record, f"evidence {field} does not match")
        record = gate(facts(edit=lambda doc: doc["run"].__setitem__("workflow_ref", "yudduy/demo/.github/workflows/verify.yml@x")))
        self.assertBlocked(record, "pinned kit verify workflow")

    def test_artifact_hash_must_match(self):
        f = facts()
        f.artifact_files["traces/save-profile.zip"] = "0" * 64
        self.assertBlocked(gate(f), "artifact traces/save-profile.zip hash does not match")

    def test_touched_journey_must_pass_on_stored_data(self):
        files = [change("src/profile/save.ts")]
        ok = gate(facts(files=files, reviews=[review("alice")]))
        self.assertEqual(ok["conclusion"], "success")
        breaks = {
            "assertion fails": lambda d: d["scenarios"][0]["data_assertions"][0].__setitem__("pass", False),
            "no assertions": lambda d: d["scenarios"][0].__setitem__("data_assertions", []),
            "ui only": lambda d: d["scenarios"][0].__setitem__("ui_result", "fail"),
            "pr-sourced": lambda d: d["scenarios"][0].__setitem__("source", "pr"),
            "absent": lambda d: d.__setitem__("scenarios", []),
        }
        for name, edit in breaks.items():
            with self.subTest(name):
                self.assertBlocked(gate(facts(files=files, reviews=[review("alice")], edit=edit)), "journey save-profile did not pass")
        untouched = gate(facts(edit=breaks["assertion fails"]))
        self.assertEqual(untouched["conclusion"], "success")

    def test_required_checks_must_be_success(self):
        for state in ("skipped", "neutral", "pending", "cancelled", "timed_out", "failure"):
            with self.subTest(state):
                record = gate(facts(checks={"test": {"github-actions": state}}))
                self.assertBlocked(record, f"required check 'test' is {state}")
                self.assertEqual(record["required_checks"], {"test": state})
        self.assertBlocked(gate(facts(checks={})), "required check 'test' is missing")

    def test_required_check_from_another_app_counts_as_missing(self):
        record = gate(facts(checks={"test": {"evil-app": "success"}}))
        self.assertEqual(record["required_checks"], {"test": "missing"})
        self.assertBlocked(record, "required check 'test' is missing")

    def test_kit_workflow_pins_must_equal_policy_kit_ref_d1(self):
        for pins in ({".github/workflows/verify.yml": ["9" * 40]}, {".github/workflows/verify.yml": ["main"]},
                     {".github/workflows/verify.yml": [KIT], ".github/workflows/other.yml": ["9" * 40]}):
            with self.subTest(pins):
                self.assertBlocked(gate(facts(kit_pins=pins)), "does not equal the policy kit_ref")

    def test_unsupported_evidence_schema_fails_closed(self):
        for schema in (-1, 0, 2, "1", None):
            with self.subTest(schema):
                record = gate(facts(edit=lambda doc: doc.__setitem__("schema", schema)))
                self.assertEqual(record["evidence"], "unsupported-schema")
                self.assertBlocked(record, "bump kit to a supported version")

    def test_missing_or_malformed_evidence_fails(self):
        self.assertEqual(gate(facts(evidence=None, attestation=None))["evidence"], "missing")
        self.assertEqual(gate(facts(evidence=b"not json"))["evidence"], "mismatch")
        self.assertEqual(gate(facts(edit=lambda doc: doc.pop("run")))["evidence"], "mismatch")

    def test_base_policy_problems_fail(self):
        self.assertBlocked(gate(facts(policy_text=None)), "no .agent-kit/policy.yml")
        record = gate(facts(policy_text="version: 1\n"))
        self.assertBlocked(record, "breaks the v1 schema")
        self.assertEqual(record["evidence"], "mismatch")

    def test_branch_state_and_forks_fail(self):
        self.assertBlocked(gate(facts(behind_by=2)), "2 commit(s) behind")
        f = facts()
        f.pr["head"]["repo"] = {"full_name": "someone/fork"}
        self.assertBlocked(gate(f), "forks are not supported")
        self.assertBlocked(gate(facts(files=[change(f"docs/{i}.md") for i in range(300)])), "300 or more files")
        bot_commit = {"author": {"login": "agent-bot[bot]"}, "committer": {"login": "web-flow"}}
        self.assertBlocked(gate(facts(commits=[bot_commit] * 250)), "250 or more commits")

    def test_only_prs_into_the_default_branch_pass_inv3(self):
        f = facts()
        f.pr["base"]["ref"] = "agent-base"
        self.assertBlocked(gate(f), "only pull requests into main are gated")

    def test_rename_out_of_a_journey_area_still_needs_the_journey(self):
        moved = [change("docs/save.md", status="renamed", previous_filename="src/profile/save.ts")]
        record = gate(facts(files=moved, edit=lambda d: d.__setitem__("scenarios", [])))
        self.assertEqual(record["class"], "review")
        self.assertBlocked(record, "journey save-profile did not pass")

    def test_paused_blocks_and_shadow_says_would_not(self):
        record = gate(facts(paused=True))
        self.assertBlocked(record, "paused")
        self.assertEqual(record["shadow"], "would_not")

    def test_shadow_verdict_for_error_fix(self):
        record = gate(facts(files=[change("src/app.py")]))
        self.assertEqual((record["conclusion"], record["shadow"]), ("failure", "would_merge"))
        self.assertIn("would merge it", summary(record)[1])
        record = gate(facts(files=[change("src/app.py")], checks={"test": {"github-actions": "failure"}}))
        self.assertEqual(record["shadow"], "would_not")

    def test_record_has_c5_fields(self):
        self.assertEqual(list(gate(facts())), ["v", "repo", "pr", "head_sha", "base_sha", "kit_ref", "policy_sha", "class",
                                                "classes_hit", "evidence", "required_checks", "approval", "conclusion", "shadow", "reasons"])
        self.assertEqual(gate(facts())["base_sha"], BASE)


if __name__ == "__main__":
    unittest.main()
