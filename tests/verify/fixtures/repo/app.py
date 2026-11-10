"""Fixture app for verify tests: one profile, a form to rename it, SQLite storage.

The line marked `planted-bug` is the write; make_repo.py deletes it to plant the bug
"the save handler returns 200 and says Saved without writing".
"""
import html
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

DB = os.environ["DATABASE_URL"].removeprefix("sqlite://")
USER = "seed-user-1"


def save_name(name):
    with sqlite3.connect(DB) as db:
        db.execute("update profiles set name = ? where id = ?", (name, USER))


def current_name():
    with sqlite3.connect(DB) as db:
        return db.execute("select name from profiles where id = ?", (USER,)).fetchone()[0]


class Handler(BaseHTTPRequestHandler):
    def reply(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply("ok")
        self.reply(f"""<form method="post" action="/save">
<p>Current name: <span id="current">{html.escape(current_name())}</span></p>
<label>Name <input name="name"></label> <button>Save</button></form>""")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        name = parse_qs(self.rfile.read(length).decode()).get("name", [""])[0]
        save_name(name)  # planted-bug
        self.reply('<p id="status">Saved</p><a href="/">Back</a>')

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
