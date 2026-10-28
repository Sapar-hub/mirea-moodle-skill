#!/usr/bin/env python3
"""Offline tests for the opt-in write path.

These tests never touch production. They run the real CLI against a local stub
HTTP server and a synthetic capture.jsonl, so export/share/solve can be checked
without Moodle, Chrome or a SyncShare account.

Run from the skill directory:
    python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "audit.py"


def review_body(quiz: str = "100", attempt: str = "7", with_essay: bool = False) -> dict:
    questions = [
        {
            "id": 10,
            "identity": "123456",
            "__type": "mc_single",
            "behavior": "deferredfeedback",
            "state": "correct",
            "grade": [1.0],
            "formulation": {"html": "<p>2 + 2?</p>", "images": []},
            "options": [{"index": 0, "text": "3"}, {"index": 1, "text": "4"}],
            "feedback": None,
            "single": True,
        }
    ]
    if with_essay:
        questions.append({
            "id": 11,
            "identity": "999999",
            "__type": "essay",
            "behavior": "manualgraded",
            "state": "correct",
            "grade": [3.0],
            "formulation": {"html": "<p>Write an essay</p>", "images": []},
            "userInput": {"value": "my private answer", "state": 2},
            "feedback": None,
        })
    return {
        "moodleId": "42",
        "quizMeta": {
            "host": "online-edu.mirea.ru",
            "courseId": "1",
            "courseName": "Course",
            "quizId": quiz,
            "quizName": "Quiz",
            "attemptId": attempt,
        },
        "questions": questions,
        "reviewSummary": "<table class=\"quizreviewsummary\"></table>",
    }


def capture_line(method: str, url: str, body: dict) -> str:
    return json.dumps({
        "ts": "2026-10-27T00:00:00Z",
        "method": method,
        "url": url,
        "headers": {},
        "body": json.dumps(body, ensure_ascii=False),
    }, ensure_ascii=False)


class StubHandler(BaseHTTPRequestHandler):
    posts: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        type(self).posts.append({
            "path": self.path,
            "body": json.loads(raw.decode("utf-8") or "{}"),
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("X-Request-Ref", "test-ref")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence the stub
        pass


class ShareCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubHandler.posts = []
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.audit = root / "audit"
        self.mirea = root / "mirea"
        self.audit.mkdir()
        self.mirea.mkdir()
        self.env = os.environ.copy()
        self.env["SYNC_AUDIT_DIR"] = str(self.audit)
        self.env["MIREA_DIR"] = str(self.mirea)
        self.env.pop("SYNCSHARE_API_KEY", None)

    def tearDown(self):
        self.tmp.cleanup()

    def write_capture(self, *lines: str) -> None:
        (self.audit / "capture.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def posts(self) -> list[dict]:
        return json.loads((self.audit / "posts.json").read_text(encoding="utf-8"))["posts"]

    # ------------------------------------------------------------------ #

    def test_export_dedupes_identical_payloads(self):
        body = review_body()
        self.write_capture(
            capture_line("POST", "/api/v2/quiz/review", body),
            capture_line("POST", "/api/v2/quiz/review", body),
            capture_line("POST", "/api/v2/quiz/attempt", {"quizMeta": {
                "host": "online-edu.mirea.ru", "courseId": "1", "courseName": "C",
                "quizId": "100", "quizName": "Q", "attemptId": "7",
            }, "questions": [{"id": 10, "identity": "123456", "__type": "mc_single"}]}),
        )
        res = self.run_cli("export")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual({p["endpoint"] for p in self.posts()}, {"review", "attempt"})

    def test_export_drops_essay_by_default(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body(with_essay=True)))
        res = self.run_cli("export")
        self.assertEqual(res.returncode, 0, res.stderr)
        questions = self.posts()[0]["body"]["questions"]
        self.assertEqual([q["__type"] for q in questions], ["mc_single"])

        res = self.run_cli("export", "--include-essay")
        self.assertEqual(res.returncode, 0, res.stderr)
        questions = self.posts()[0]["body"]["questions"]
        self.assertEqual([q["__type"] for q in questions], ["mc_single", "essay"])

    def test_share_is_dry_run_without_send(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body()))
        self.run_cli("export")
        res = self.run_cli("share", "--endpoint", "review", "--host", "online-edu.mirea.ru")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[dry-run]", res.stdout)
        self.assertEqual(StubHandler.posts, [])
        self.assertFalse((self.audit / "shared.json").exists())

    def test_share_sends_once_then_skips_ledger(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body()))
        self.run_cli("export")
        args = (
            "share", "--endpoint", "review", "--send", "--delay", "0",
            "--host", "online-edu.mirea.ru", "--base-url", self.base,
            "--allow-custom-base",
        )
        res = self.run_cli(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(StubHandler.posts), 1)
        posted = StubHandler.posts[0]
        self.assertEqual(posted["path"], "/v2/quiz/review")
        self.assertEqual(posted["body"]["quizMeta"]["quizId"], "100")

        ledger = json.loads((self.audit / "shared.json").read_text(encoding="utf-8"))
        self.assertEqual(list(ledger["posts"].values())[0]["status"], "ok")

        res = self.run_cli(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(StubHandler.posts), 1, "ledger should skip a repeat")

        res = self.run_cli(*args, "--force")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(StubHandler.posts), 2, "--force should re-send")

    def test_share_refuses_foreign_host(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body()))
        self.run_cli("export")
        res = self.run_cli("share", "--endpoint", "review", "--send", "--host", "evil.example")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refusing", res.stderr)
        self.assertEqual(StubHandler.posts, [])

    def test_share_requires_custom_base_flag(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body()))
        self.run_cli("export")
        res = self.run_cli(
            "share", "--endpoint", "review", "--send", "--delay", "0",
            "--host", "online-edu.mirea.ru", "--base-url", self.base,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--allow-custom-base", res.stderr)
        self.assertEqual(StubHandler.posts, [])

    def test_solve_uses_captured_question(self):
        self.write_capture(capture_line("POST", "/api/v2/quiz/review", review_body()))
        self.run_cli("export")
        res = self.run_cli(
            "solve", "--quiz", "100", "--question-id", "10",
            "--api-key", "test-key", "--delay", "0",
            "--base-url", self.base, "--allow-custom-base",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(StubHandler.posts), 1)
        posted = StubHandler.posts[0]
        self.assertEqual(posted["path"], "/v2/quiz/solve")
        self.assertEqual(posted["headers"].get("x-api-key"), "test-key")
        self.assertEqual(posted["body"]["quizMeta"]["quizId"], "100")
        self.assertEqual(posted["body"]["question"]["id"], 10)
        self.assertEqual(posted["body"]["questionIdentity"], "123456")


if __name__ == "__main__":
    unittest.main()
