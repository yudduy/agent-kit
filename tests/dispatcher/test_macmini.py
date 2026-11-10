import unittest
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

    def dispatcher_says(self, kind, payload):
        self.gh.comment(CTL, 2, m.json_marker(kind, payload), login=DISPATCHER_LOGIN)

    def client(self, status="RUNNING"):
        client = mock.Mock()
        client.latest_run.return_value = {"id": "run-1", "status": status}
        client.steer.return_value = {"delivery": "steered", "runId": "run-1"}
        return client

    def test_steer_follows_up_the_current_claimed_run(self):
        self.dispatcher_says("claim", {"attempt": "a", "at": "x"})
        self.dispatcher_says("run", {"attempt": "a", "cursor_id": "bc-a"})
        client = self.client()
        macmini.steer(self.gh, client, CTL, 2, "Also cover empty names")
        client.steer.assert_called_once_with("bc-a", "Also cover empty names")
        client.create.assert_not_called()

    def test_steer_refuses_without_a_dispatcher_run(self):
        client = self.client()
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")
        self.gh.comment(CTL, 2, m.json_marker("claim", {"attempt": "z", "at": "x"}), login="mallory")
        self.gh.comment(CTL, 2, m.json_marker("run", {"attempt": "z", "cursor_id": "bc-evil"}), login="mallory")
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")
        client.steer.assert_not_called()

    def test_steer_refuses_a_run_from_an_earlier_generation_or_claim(self):
        self.dispatcher_says("claim", {"attempt": "a", "at": "x"})
        self.dispatcher_says("run", {"attempt": "a", "cursor_id": "bc-a"})
        self.dispatcher_says("reopen", {"last_seen": "x"})
        client = self.client()
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")
        self.dispatcher_says("claim", {"attempt": "b", "at": "x"})
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")  # the new claim has no run yet
        client.steer.assert_not_called()

    def test_a_steer_that_lands_after_the_run_ended_has_its_new_run_cancelled(self):
        self.dispatcher_says("claim", {"attempt": "a", "at": "x"})
        self.dispatcher_says("run", {"attempt": "a", "cursor_id": "bc-a"})
        client = mock.Mock()
        client.latest_run.side_effect = [{"id": "run-1", "status": "RUNNING"}, {"id": "run-2", "status": "CREATING"}]
        client.steer.return_value = {"delivery": "queued"}
        with self.assertRaises(SystemExit) as stop:
            macmini.steer(self.gh, client, CTL, 2, "hi")
        client.cancel.assert_called_once_with("bc-a", "run-2")
        self.assertIn("cancelled", str(stop.exception))

    def test_a_steered_prompt_never_cancels_a_run_it_did_not_start(self):
        self.dispatcher_says("claim", {"attempt": "a", "at": "x"})
        self.dispatcher_says("run", {"attempt": "a", "cursor_id": "bc-a"})
        client = mock.Mock()
        client.latest_run.side_effect = [{"id": "run-1", "status": "RUNNING"}, {"id": "run-2", "status": "RUNNING"}]
        client.steer.return_value = {"delivery": "steered", "runId": "run-1"}
        macmini.steer(self.gh, client, CTL, 2, "hi")
        client.cancel.assert_not_called()

    def test_steer_refuses_an_idle_agent_because_that_would_start_a_run(self):
        self.dispatcher_says("claim", {"attempt": "a", "at": "x"})
        self.dispatcher_says("run", {"attempt": "a", "cursor_id": "bc-a"})
        client = self.client("FINISHED")
        with self.assertRaises(SystemExit):
            macmini.steer(self.gh, client, CTL, 2, "hi")
        client.steer.assert_not_called()

if __name__ == "__main__":
    unittest.main()
