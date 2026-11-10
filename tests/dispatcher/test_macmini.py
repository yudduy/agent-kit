import ast
import unittest
from pathlib import Path
from unittest import mock

from dispatcher import macmini, markers as m
from dispatcher.tick import DISPATCHER_LOGIN
from .fakes import Clock, FakeGitHub

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

    def test_steer_only_posts_a_request_comment(self):
        macmini.steer(self.gh, CTL, 2, "Also cover empty names\n")
        comment = self.gh.comments_[2][-1]
        self.assertEqual(m.steer_request(comment, ["yudduy-mini"])["prompt"], "Also cover empty names")

    def test_the_helper_never_talks_to_cursor(self):
        source = Path(macmini.__file__).read_text()
        imported = {n.name for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom)) for n in node.names}
        self.assertNotIn("cursor", imported)
        self.assertNotIn("CURSOR_API_KEY", source)

if __name__ == "__main__":
    unittest.main()
