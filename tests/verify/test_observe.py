import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_repo  # noqa: E402
from verify import observe  # noqa: E402

SEED = (make_repo.SRC / ".agent-kit" / "seed" / "seed.sql").read_text()
ASSERTION = {"id": "name-persisted", "sql": "select name from profiles where id = 'seed-user-1'",
             "expect": [{"name": "Ada"}]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ObserverCatchesUnsavedWrites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repo = cls.tmp / "repo"
        cls.shas = make_repo.make(cls.repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def save_through_ui(self, commit):
        """Start the fixture app at `commit`, submit the form, return (http reply, db url)."""
        src = self.tmp / commit
        src.mkdir()
        subprocess.run(f"git -C {self.repo} archive {self.shas[commit]} | tar -x -C {src}", shell=True, check=True)
        url = f"sqlite://{self.tmp / commit}.sqlite"
        observe.load_seed("sqlite", url, SEED)
        port = free_port()
        app = subprocess.Popen([sys.executable, "app.py"], cwd=src,
                               env={**os.environ, "PORT": str(port), "DATABASE_URL": url})
        self.addCleanup(app.wait)
        self.addCleanup(app.terminate)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        form = urllib.parse.urlencode({"name": "Ada"}).encode()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/save", data=form, timeout=5) as r:
            return r.status, r.read().decode(), url

    def test_ui_says_saved_but_db_unchanged_fails(self):
        status, body, url = self.save_through_ui("mutant")
        self.assertEqual(status, 200)
        self.assertIn("Saved", body)
        [result] = observe.run_assertions("sqlite", url, [ASSERTION])
        self.assertFalse(result["pass"])
        self.assertEqual(result["observed"], [{"name": "Grace"}])

    def test_real_save_passes(self):
        _, _, url = self.save_through_ui("fix")
        [result] = observe.run_assertions("sqlite", url, [ASSERTION])
        self.assertTrue(result["pass"])
        self.assertEqual(len(result["sql_sha256"]), 64)


class ObserverEdges(unittest.TestCase):
    def test_observer_cannot_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite://{tmp}/db.sqlite"
            observe.load_seed("sqlite", url, SEED)
            [result] = observe.run_assertions("sqlite", url, [
                {"id": "w", "sql": "update profiles set name = 'Ada'", "expect": []}])
            self.assertIsNone(result["observed"])
            self.assertEqual(observe.query("sqlite", url, ASSERTION["sql"]), [{"name": "Grace"}])

    def test_other_engines_are_unsupported(self):
        with self.assertRaises(observe.Unsupported):
            observe.query("mysql", "mysql://localhost/db", "select 1")


@unittest.skipUnless(shutil.which("initdb") and shutil.which("pg_ctl") and shutil.which("psql"),
                     "needs a local postgres install")
class PostgresObserver(unittest.TestCase):
    def test_seed_query_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, port = Path(tmp) / "data", free_port()
            subprocess.run(["initdb", "-A", "trust", "-U", "postgres", "-D", data], check=True, capture_output=True)
            subprocess.run(["pg_ctl", "-D", data, "-l", Path(tmp) / "log", "-w", "start",
                            "-o", f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1"], check=True,
                           stdout=subprocess.DEVNULL)
            try:
                url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
                observe.load_seed("postgres", url, SEED)
                [ok] = observe.run_assertions("postgres", url, [{**ASSERTION, "expect": [{"name": "Grace"}]}])
                self.assertTrue(ok["pass"])
                observe.load_seed("postgres", url, "create sequence s;")
                with self.assertRaisesRegex(RuntimeError, "read-only transaction"):
                    observe.query("postgres", url, "select nextval('s') as n")
                self.assertEqual(observe.query("postgres", url, "select count(*) as n from profiles"), [{"n": 1}])
            finally:
                subprocess.run(["pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
