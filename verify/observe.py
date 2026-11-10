"""Kit-owned observer: loads the seed and runs data assertions straight against the database.

Candidate code never sits between the observer and the data. Supported engines:
- sqlite: stdlib sqlite3; url is ``sqlite:///<absolute path>``.
- postgres: the ``psql`` CLI; url is a libpq connection URL.
Any other engine is unsupported and raises ``Unsupported``.
"""
import json
import os
import sqlite3
import subprocess

from verify.evidence import sha256_bytes

ENGINES = ("sqlite", "postgres")


class Unsupported(ValueError):
    pass


def _check(engine):
    if engine not in ENGINES:
        raise Unsupported(f"database engine {engine!r} is not supported; use one of {', '.join(ENGINES)}")


def sqlite_path(url):
    if not url.startswith("sqlite:///"):
        raise ValueError(f"sqlite url must look like sqlite:///<path>, got {url!r}")
    return url.removeprefix("sqlite://")


def psql(url, *args, read_only=False, input_text=None):
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
    if read_only:
        env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    proc = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url, *args],
                          env=env, input=input_text, capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"psql exited {proc.returncode}")
    return proc.stdout


def load_seed(engine, url, seed_sql):
    _check(engine)
    if engine == "sqlite":
        with sqlite3.connect(sqlite_path(url)) as db:
            db.executescript(seed_sql)
    else:
        psql(url, "-f", "-", input_text=seed_sql)


def query(engine, url, sql):
    """Return the rows of ``sql`` as a list of dicts, read-only."""
    _check(engine)
    if engine == "sqlite":
        db = sqlite3.connect(f"file:{sqlite_path(url)}?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql)]
        finally:
            db.close()
    wrapped = f"select coalesce(json_agg(t), '[]'::json) from ({sql.rstrip().rstrip(';')}) t"
    return json.loads(psql(url, "-A", "-t", "-c", wrapped, read_only=True))


def run_assertions(engine, url, assertions):
    """Evaluate policy data assertions; each becomes a C2 data_assertions entry."""
    results = []
    for a in assertions:
        try:
            observed = query(engine, url, a["sql"])
        except (sqlite3.Error, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"observe: assertion {a['id']} could not run: {exc}")
            observed = None
        results.append({"id": a["id"], "sql_sha256": sha256_bytes(a["sql"].encode()),
                        "expected": a["expect"], "observed": observed, "pass": observed == a["expect"]})
    return results
