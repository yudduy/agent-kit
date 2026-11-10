"""Build a git repo from fixtures/repo with three commits and print their SHAs as JSON.

bug:    the app says "Saved" without writing (base for the fix).
fix:    the write restored, plus a new regression spec under tests/regress/.
mutant: the fix with the write deleted again ("UI says saved, DB unchanged").
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "repo"
REGRESS = "tests/regress"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def _plant_bug(repo):
    app = repo / "app.py"
    app.write_text("".join(line for line in app.read_text().splitlines(True) if "# planted-bug" not in line))


def make(dest):
    dest = Path(dest)
    shutil.copytree(SRC, dest)
    _git(dest, "init", "-q", "-b", "main")
    shutil.move(dest / REGRESS, dest.parent / f"{dest.name}-regress")
    _plant_bug(dest)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "bug")
    shutil.copy(SRC / "app.py", dest / "app.py")
    shutil.move(dest.parent / f"{dest.name}-regress", dest / REGRESS)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fix")
    _plant_bug(dest)
    _git(dest, "commit", "-q", "-am", "mutant")
    return {name: _git(dest, "rev-parse", f"HEAD~{n}") for name, n in (("bug", 2), ("fix", 1), ("mutant", 0))}


if __name__ == "__main__":
    print(json.dumps(make(sys.argv[1])))
