"""pytest plugin loaded by learning/proof.py through PYTEST_ADDOPTS. Writes one JSON report to $AGENT_KIT_PROOF_REPORT:
each test's call outcome ("passed", "skipped", "assertion" for an AssertionError or pytest.fail, "error" for anything
else), collection, setup and teardown errors, and how many assertions passed (plain `assert` through pytest's
assertion-pass hook, and unittest `assert*` methods)."""
import json
import os
import unittest

import pytest

STATE = {"tests": [], "errors": [], "assertions": 0}


def _counted(method):
    def wrapper(*args, **kwargs):
        result = method(*args, **kwargs)
        STATE["assertions"] += 1
        return result
    return wrapper


for _name in [n for n in vars(unittest.TestCase) if n.startswith("assert") and callable(getattr(unittest.TestCase, n))]:
    setattr(unittest.TestCase, _name, _counted(getattr(unittest.TestCase, _name)))


def pytest_assertion_pass(item, lineno, orig, expl):
    STATE["assertions"] += 1


def pytest_collectreport(report):
    if report.failed:
        STATE["errors"].append("collect %s" % report.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if report.when == "call":
        if report.passed or report.skipped:
            outcome = report.outcome
        elif call.excinfo.errisinstance((AssertionError, pytest.fail.Exception)):
            outcome = "assertion"
        else:
            outcome = "error"
        STATE["tests"].append({"id": report.nodeid, "outcome": outcome})
    elif report.failed:
        STATE["errors"].append("%s %s" % (report.when, report.nodeid))


def pytest_sessionfinish(session):
    with open(os.environ["AGENT_KIT_PROOF_REPORT"], "w") as handle:
        json.dump(STATE, handle)
