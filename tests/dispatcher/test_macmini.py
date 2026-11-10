import unittest
from unittest import mock

from dispatcher import macmini, markers as m
from fakes import Clock, FakeGitHub

CTL = "yudduy/agent-control"


class MacMiniHelper(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub(Clock(), login="yudduy-mini")
        self.gh.create_issue(CTL, "Mac mini heartbeat", "", [])
        self.gh.create_issue(CTL, "Sentry error", m.intake("f" * 16, "sentry", "1", "x", "yudduy/shop"), [m.INCIDENT, m.NEEDS_DECISION])

    def test_heartbeat_is_edited_in_place(self):
        macmini.heartbeat(self.gh, CTL, 1)
        self.gh.clock.advance(5)
        macmini.heartbeat(self.gh, CTL, 1, accepting=False)
        beats = self.gh.comments_[1]
        self.assertEqual(len(beats), 1)
        self.assertFalse(m.parse("heartbeat", beats[0]["body"])["accepting"])
        self.assertNotEqual(beats[0]["created_at"], beats[0]["updated_at"])

    def test_decision_round_trips_to_the_dispatcher(self):
        self.assertEqual([i["number"] for i in macmini.pending(self.gh, CTL)], [2])
        macmini.decide(self.gh, CTL, 2, "fix", "Fix the null name.\n", "composer-2")
        self.assertEqual(macmini.pending(self.gh, CTL), [])
        decision = m.decision(self.gh.comments_[2][0], ["yudduy-mini"])
        self.assertEqual((decision["action"], decision["prompt"]), ("fix", "Fix the null name."))

    def test_steer_targets_the_recorded_run_and_never_creates(self):
        client = mock.Mock()
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")
        self.gh.comment(CTL, 2, m.json_marker("run", {"attempt": "a", "cursor_id": "bc-a"}), login="github-actions[bot]")
        macmini.steer(self.gh, client, CTL, 2, "Also cover empty names")
        client.steer.assert_called_once_with("bc-a", "Also cover empty names")
        client.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
