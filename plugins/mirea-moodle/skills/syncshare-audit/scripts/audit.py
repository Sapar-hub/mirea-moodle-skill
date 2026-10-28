#!/usr/bin/env python3
"""SyncShare coverage audit for Moodle quizzes, with an opt-in write path.

Subcommands:
  inventory   discover quizzes and their windows/attempts
  fetch       download attempt/review pages (may start an attempt)
  harness     run the real extension against saved pages in a local mock
  identities  extract questionIdentity values from the captured requests
  export      extract complete write payloads from capture.jsonl
  share       replay exported payloads to production (dry-run unless --send)
  solve       ask quiz/solve about one captured question (never touches Moodle)
  query       query the public quiz/solution API (GET only) and classify
  probe       verify the lookup scope (host is the key, ids are ignored)
  report      render a per-quiz coverage table
  run         fetch + harness + identities + query

The GET audit is read-only. `share` and `solve` are explicit opt-ins: they send
question text and answers to SyncShare, and `share` has no undo. `run` never
calls a write endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://online-edu.mirea.ru"
API_BASE = "https://syncshare.ru/api/"
API_URL = API_BASE + "v2/quiz/solution"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

AUDIT = Path(os.environ.get("SYNC_AUDIT_DIR", str(Path.home() / ".syncshare-audit")))
MIREA = Path(os.environ.get("MIREA_DIR", str(Path.home() / ".mirea")))

PAGES = AUDIT / "pages"
VIEWS = AUDIT / "view"
HARNESS = AUDIT / "harness"
EXT = AUDIT / "extension"
CAPTURE = AUDIT / "capture.jsonl"
INVENTORY = AUDIT / "inventory.json"
IDENTITIES = AUDIT / "identities.json"
RESULTS = AUDIT / "results.json"
PROBE = AUDIT / "probe.json"
POSTS = AUDIT / "posts.json"
SHARED = AUDIT / "shared.json"
SOLVE = AUDIT / "solve.json"
CAPTURES = AUDIT / "captures"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for p in (AUDIT, PAGES, VIEWS):
        p.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError:
        return default


def canonical_json(obj) -> str:
    """Stable representation for hashing/deduplicating POST bodies."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_essay(question: dict) -> bool:
    return "essay" in str(question.get("__type") or "").lower()


def filter_questions(body: dict, include_essay: bool) -> tuple[dict, int]:
    """Return a copy of a captured POST body without essay questions."""
    if include_essay:
        return body, 0
    out = json.loads(json.dumps(body))  # cheap deep copy
    questions = out.get("questions")
    if not isinstance(questions, list):
        return out, 0
    kept = [q for q in questions if not is_essay(q)]
    dropped = len(questions) - len(kept)
    out["questions"] = kept
    return out, dropped


def text_of(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).strip()


def title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return ""
    title = htmlmod.unescape(m.group(1)).strip()
    return re.split(r"\s+\|\s+", title)[0].strip()


def count_que(html: str) -> int:
    return len(re.findall(r'class="que\s+[a-z0-9_]+', html))


def qtypes(html: str) -> dict:
    out: dict[str, int] = {}
    for t in re.findall(r'class="que\s+([a-z0-9_]+)', html):
        out[t] = out.get(t, 0) + 1
    return out


def sesskey() -> str:
    p = MIREA / "sesskey"
    if not p.exists() or not read_text(p).strip():
        die("no sesskey — run the mirea-moodle skill: start + otp")
    return read_text(p).strip()


def moodle_id() -> str:
    for name in ("my.html", "prof.html"):
        p = MIREA / name
        if not p.exists():
            continue
        html = read_text(p)
        for pattern in (r"user/profile\.php\?id=(\d+)", r'data-userid="(\d+)"'):
            m = re.search(pattern, html)
            if m:
                return m.group(1)
    return os.environ.get("MOODLE_ID", "")


def curl(url: str, out: Path | None = None, data: dict | None = None,
         follow: bool = False, timeout: int = 45) -> str:
    ensure_dirs()
    cmd = [
        "curl", "--noproxy", "*", "-4", "-sS", "-m", str(timeout),
        "-A", UA, "-b", str(MIREA / "jar.txt"), "-c", str(MIREA / "jar.txt"),
    ]
    if follow:
        cmd.append("-L")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(out), "-w", "%{url_effective}"]
    if data:
        for k, v in data.items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"curl failed ({res.returncode}): {res.stderr.strip()[:300]}")
    return res.stdout.strip()


def get_page(url: str, out: Path, refresh: bool = False) -> str:
    if out.exists() and not refresh:
        return read_text(out)
    curl(url, out=out)
    return read_text(out)


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


def load_courses() -> list[tuple[str, str]]:
    p = MIREA / "courses.json"
    if not p.exists():
        die("no courses.json — run: mirea-moodle courses")
    data = load_json(p, [])
    if isinstance(data, list) and data and isinstance(data[0], dict) and "data" in data[0]:
        data = data[0].get("data") or {}
    courses = data.get("courses", []) if isinstance(data, dict) else []
    if not courses:
        die("courses.json has no courses — re-run mirea-moodle courses")
    return [(str(c["id"]), str(c.get("fullname") or c.get("shortname") or c["id"]))
            for c in courses]


def parse_view(html: str) -> dict:
    text = text_of(html)
    attempts = re.search(r"Разрешено попыток:\s*(\d+)", text)
    time_limit = re.search(r"Ограничение по времени:\s*([^<]+?)\s+(?:Проходная|Метод|Вернуться)", text)
    total = re.search(r"В тесте представлено\s+(\d+)\s+вопрос", text)
    opens = re.search(r"Открыто с:\s*([^<]+?)\s+Закрыто", text)
    closes = re.search(r"Закрыто c:\s*([^<]+?)\s+", text)
    reviews = sorted(set(re.findall(r"review\.php\?attempt=(\d+)", html)))
    in_progress = sorted(set(re.findall(r"attempt\.php\?attempt=(\d+)", html)))
    course = re.search(r"\bcourse-(\d+)\b", html)
    return {
        "title": title_of(html),
        "course_id": course.group(1) if course else "",
        "attempts_limit": int(attempts.group(1)) if attempts else None,
        "time_limit": time_limit.group(1).strip() if time_limit else None,
        "total_questions": int(total.group(1)) if total else None,
        "opens": opens.group(1).strip() if opens else None,
        "closes": closes.group(1).strip() if closes else None,
        "startable": "startattempt.php" in html,
        "reviews": reviews,
        "in_progress": in_progress,
    }


def cmd_inventory(args) -> None:
    ensure_dirs()
    all_courses = load_courses()
    wanted = set(args.course or [])
    if wanted:
        missing = wanted - {cid for cid, _ in all_courses}
        if missing:
            die(f"course(s) not in courses.json: {', '.join(sorted(missing))}")
        all_courses = [(cid, name) for cid, name in all_courses if cid in wanted]

    inv = {
        "generated": now(),
        "host": urllib.parse.urlparse(BASE).netloc,
        "moodle_id": moodle_id(),
        "quizzes": {},
        "courses": {},
    }

    for cid, cname in all_courses:
        page = MIREA / f"course_{cid}.html"
        if args.refresh or not page.exists():
            curl(f"{BASE}/course/view.php?id={cid}", out=page)
        html = read_text(page)
        qids = sorted(set(re.findall(r"mod/quiz/view\.php\?id=(\d+)", html)), key=int)
        inv["courses"][cid] = {"name": cname, "quizzes": qids}
        if not qids:
            print(f"{cid} {cname}: no quizzes")
            continue
        print(f"{cid} {cname}: {len(qids)} quiz(es)")
        for qid in qids:
            vp = VIEWS / f"{qid}.html"
            if args.refresh or not vp.exists():
                curl(f"{BASE}/mod/quiz/view.php?id={qid}", out=vp)
            meta = parse_view(read_text(vp))
            meta.update({"cmid": qid, "course_id": cid, "course_name": cname,
                         "url": f"{BASE}/mod/quiz/view.php?id={qid}"})
            inv["quizzes"][qid] = meta
            flags = []
            if not meta["startable"]:
                flags.append("closed/restricted")
            if meta["reviews"]:
                flags.append("review=" + ",".join(meta["reviews"]))
            if meta["in_progress"]:
                flags.append("running=" + ",".join(meta["in_progress"]))
            if meta["attempts_limit"] == 1:
                flags.append("1-attempt")
            print(
                f"  {qid}  {meta['title'][:60]:60}  "
                f"q={meta['total_questions'] or '?'}  "
                f"{'; '.join(flags) or 'open'}"
            )

    write_json(INVENTORY, inv)
    print(f"\nwrote {INVENTORY}")


# --------------------------------------------------------------------------- #
# fetch pages
# --------------------------------------------------------------------------- #


def fetch_pages(cmid: str, attempt: str, total: int, mode: str,
                first_html: str, extra: dict | None = None) -> dict:
    qdir = PAGES / cmid
    qdir.mkdir(parents=True, exist_ok=True)
    if count_que(first_html) == 0:
        die(f"{cmid}: no questions on the first page — session expired, "
            f"review unavailable, or this is not a quiz attempt")
    for old in qdir.glob(f"{attempt}_p*.html"):
        old.unlink()
    (qdir / f"{attempt}_p0.html").write_text(first_html, encoding="utf-8")

    page_links = [int(x) for x in re.findall(r"page=(\d+)", first_html)]
    if page_links:
        pages = max(page_links) + 1
    elif total and count_que(first_html) >= total:
        pages = 1
    elif total:
        pages = total
    else:
        pages = 1
    pages = max(1, min(pages, 100))

    for p in range(1, pages):
        out = qdir / f"{attempt}_p{p}.html"
        if mode == "review":
            url = f"{BASE}/mod/quiz/review.php?attempt={attempt}&cmid={cmid}&page={p}"
        else:
            url = f"{BASE}/mod/quiz/attempt.php?attempt={attempt}&cmid={cmid}&page={p}"
        curl(url, out=out)
        html = read_text(out)
        if count_que(html) == 0:
            out.unlink(missing_ok=True)
            print(f"  page {p}: 0 questions (stopping)")
            break

    files = sorted(qdir.glob(f"{attempt}_p*.html"))
    kinds: dict[str, int] = {}
    for f in files:
        for k, v in qtypes(read_text(f)).items():
            kinds[k] = kinds.get(k, 0) + v

    meta = {
        "cmid": cmid,
        "attempt": attempt,
        "mode": mode,
        "pages": len(files),
        "total_questions": total,
        "qtypes": kinds,
        "fetched_at": now(),
    }
    meta.update(extra or {})
    write_json(qdir / "meta.json", meta)
    print(f"  saved {len(files)} page(s), types={kinds or '-'}")
    return meta


def cmd_fetch(args) -> None:
    inv = load_json(INVENTORY, {}) or {}
    quizzes = inv.get("quizzes", {})
    failures = 0
    for cmid in args.quiz:
        # Always refresh the view page: attempt/review availability changes
        # while the audit runs, and a stale inventory could make us start an
        # attempt that is no longer needed (or consume a single attempt).
        base = quizzes.get(cmid, {}) or {}
        vp = VIEWS / f"{cmid}.html"
        effective = curl(f"{BASE}/mod/quiz/view.php?id={cmid}", out=vp)
        if "login" in effective or "loginform" in read_text(vp):
            die("Moodle session expired — re-run mirea-moodle check/start/otp")
        fresh = parse_view(read_text(vp))
        meta = {**base, **fresh}
        meta.update({
            "cmid": cmid,
            "course_id": base.get("course_id") or fresh.get("course_id") or args.course or "",
            "course_name": base.get("course_name", ""),
            "title": fresh.get("title") or base.get("title", ""),
        })
        quizzes[cmid] = meta
        print(f"\n[{cmid}] {meta.get('title', '')}")

        if meta.get("in_progress") and not args.new_attempt:
            attempt = meta["in_progress"][0]
            print(f"  resuming in-progress attempt {attempt}")
            out = PAGES / cmid / f"{attempt}_p0.html"
            curl(f"{BASE}/mod/quiz/attempt.php?attempt={attempt}&cmid={cmid}", out=out)
            fetch_pages(cmid, attempt, meta.get("total_questions") or 0, "resume",
                        read_text(out),
                        {"course_id": meta.get("course_id") or args.course or "",
                         "title": meta.get("title") or ""})
            continue

        if meta.get("reviews") and not args.new_attempt:
            attempt = meta["reviews"][0]
            print(f"  using review attempt {attempt} (no new attempt created)")
            out = PAGES / cmid / f"{attempt}_p0.html"
            curl(f"{BASE}/mod/quiz/review.php?attempt={attempt}&cmid={cmid}", out=out)
            fetch_pages(cmid, attempt, meta.get("total_questions") or 0, "review",
                        read_text(out),
                        {"course_id": meta.get("course_id") or args.course or "",
                         "title": meta.get("title") or ""})
            continue

        if not meta.get("startable"):
            print("  SKIP: no start form (closed by date or restricted by prerequisites)")
            failures += 1
            continue

        if meta.get("attempts_limit") == 1 and not args.allow_single_attempt:
            print("  REFUSE: only 1 attempt allowed and no finished review.")
            print("          Starting it spends the only attempt and it will be graded 0.")
            print("          Re-run with --allow-single-attempt after explicit user consent.")
            failures += 1
            continue

        sk = sesskey()
        out = PAGES / cmid / "_start.html"
        effective = curl(
            f"{BASE}/mod/quiz/startattempt.php",
            out=out,
            data={
                "cmid": cmid,
                "sesskey": sk,
                "_qf__mod_quiz_form_preflight_check_form": "1",
            },
            follow=True,
        )
        html = read_text(out) if out.exists() else ""
        m = re.search(r"attempt=(\d+)", effective) or re.search(r"attempt=(\d+)", html)
        if not m or 'id="page-mod-quiz-attempt"' not in html:
            failures += 1
            snippet = text_of(html)[:200]
            print(f"  FAIL: could not start an attempt (effective={effective[:120]})")
            print(f"        {snippet}")
            continue
        attempt = m.group(1)
        print(f"  started attempt {attempt} — it is now open; do not submit it")
        fetch_pages(cmid, attempt, meta.get("total_questions") or 0, "attempt", html,
                    {"course_id": meta.get("course_id") or args.course or "",
                     "title": meta.get("title") or ""})
        meta["attempts_started"] = meta.get("attempts_started", []) + [attempt]
        quizzes[cmid] = meta

    if quizzes:
        inv["quizzes"] = quizzes
        write_json(INVENTORY, inv)
    if failures:
        die(f"{failures} quiz(es) could not be fetched", code=3)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def server_health() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8081/__health", timeout=1) as r:
            data = json.loads(r.read().decode())
        return (data.get("ok") is True
                and Path(str(data.get("pageDir") or "")).resolve() == PAGES.resolve()
                and Path(str(data.get("capture") or "")).resolve() == CAPTURE.resolve())
    except Exception:
        return False


def harness_env() -> dict:
    env = os.environ.copy()
    env_file = AUDIT / "env.json"
    saved = load_json(env_file, {}) or {}
    env.update({k: str(v) for k, v in saved.items() if k not in env})
    env["SYNC_AUDIT_DIR"] = str(AUDIT)
    env["PAGE_DIR"] = str(PAGES)
    env["VIEW_DIR"] = str(VIEWS)
    env["MOODLE_ID"] = moodle_id() or "1"
    env["CAPTURE"] = str(CAPTURE)
    env["CERT"] = str(HARNESS / "cert.pem")
    env["KEY"] = str(HARNESS / "key.pem")
    env["PAGE_PORT"] = "8081"
    env["API_PORT"] = "8443"
    return env


def ensure_harness() -> None:
    if not (HARNESS / "node_modules" / "playwright").exists():
        die(f"harness not installed — run scripts/setup.sh (missing {HARNESS}/node_modules/playwright)")
    if not (EXT / "manifest.json").exists():
        die(f"extension not unpacked — run scripts/setup.sh (missing {EXT}/manifest.json)")
    env = harness_env()
    chrome = env.get("CHROME_PATH", "")
    if not chrome or not Path(chrome).exists():
        die("CHROME_PATH is not set or does not exist — run scripts/setup.sh / source env.sh")
    if not (HARNESS / "cert.pem").exists() or not (HARNESS / "key.pem").exists():
        die(f"missing TLS cert in {HARNESS} — run scripts/setup.sh")


def archive_capture() -> None:
    """Keep the previous run's capture before the mock truncates it.

    `export` reads capture.jsonl, so a second harness run must not silently
    destroy the first run's write payloads.
    """
    if not CAPTURE.exists() or CAPTURE.stat().st_size == 0:
        return
    CAPTURES.mkdir(parents=True, exist_ok=True)
    dst = CAPTURES / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    shutil.copy2(CAPTURE, dst)
    print(f"archived previous capture -> {dst}")


def cmd_harness(args) -> None:
    ensure_harness()
    metas = []
    for meta_file in sorted(PAGES.glob("*/meta.json")):
        meta = load_json(meta_file, {}) or {}
        if args.quiz and meta.get("cmid") not in args.quiz:
            continue
        if meta:
            metas.append(meta)
    if not metas:
        die("no fetched pages — run: audit.py fetch --quiz <cmid>")

    inv = load_json(INVENTORY, {}) or {}
    inv_host = inv.get("host") or "online-edu.mirea.ru"
    mode = getattr(args, "mode", "review")

    env = harness_env()
    started = False
    proc = None
    if not server_health():
        if port_open(8081) or port_open(8443):
            die("port 8081/8443 is busy (another audit's mock or an unrelated process) — stop it first")
        log = open(AUDIT / "harness.log", "ab")
        proc = subprocess.Popen(["node", "server.js"], cwd=HARNESS, env=env,
                                stdout=log, stderr=log)
        started = True
        for _ in range(50):
            if server_health():
                break
            time.sleep(0.2)
        else:
            proc.terminate()
            die(f"mock server did not start; see {AUDIT / 'harness.log'}")

    archive_capture()
    CAPTURE.write_text("", encoding="utf-8")

    def urls_for(items: list[dict], which: str) -> list[str]:
        out = []
        for meta in items:
            cmid = meta["cmid"]
            attempt = meta.get("attempt")
            if which == "summary":
                attempt_id = ""
                for key in ("reviews", "in_progress"):
                    values = meta.get(key) or []
                    if values:
                        attempt_id = str(values[0])
                        break
                if not attempt_id:
                    print(f"  {cmid}: summary skipped — no finished/in-progress attempt")
                    continue
                params = {
                    "id": cmid,
                    "host": meta.get("host") or inv_host,
                    "courseId": meta.get("course_id") or "1",
                    "courseName": meta.get("course_name") or "Course",
                    "quizId": cmid,
                    "quizName": meta.get("title") or "Quiz",
                    "attemptId": attempt_id,
                    "moodleId": moodle_id() or "1",
                }
                out.append(f"http://127.0.0.1:8081/mod/quiz/view.php?{urllib.parse.urlencode(params)}")
                continue
            pages = int(meta.get("pages") or 1)
            for p in range(pages):
                if not (PAGES / cmid / f"{attempt}_p{p}.html").exists():
                    continue
                params = {
                    "attempt": attempt,
                    "cmid": cmid,
                    "page": p,
                    "course": meta.get("course_id") or "1",
                    "qname": meta.get("title") or "Quiz",
                }
                path = "attempt.php" if which == "attempt" else "review.php"
                if which == "attempt":
                    params["as"] = "attempt"
                q = urllib.parse.urlencode(params)
                out.append(f"http://127.0.0.1:8081/mod/quiz/{path}?{q}")
        return out

    def urls_for_modes(items: list[dict], modes: list[str]) -> list[str]:
        out: list[str] = []
        for which in modes:
            out.extend(urls_for(items, which))
        return out

    def per_quiz() -> dict[str, set]:
        found: dict[str, set] = {}
        for r in captured_questions():
            if r["quiz"] and r["id"] and r["identity"]:
                found.setdefault(r["quiz"], set()).add(r["id"])
        for post in captured_posts():
            if post["quiz"]:
                found.setdefault(post["quiz"], set()).add(
                    f"{post['endpoint']}:{post.get('attempt') or '-'}"
                )
        return found

    def run_pass(urls: list[str], label: str) -> None:
        cmd = ["node", "runner.js", *urls]
        if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", *cmd]
        print(f"{label}: loading {len(urls)} page(s) through the extension ...")
        subprocess.run(cmd, cwd=HARNESS, env=env, check=True)

    modes = {
        "review": ["review"],
        "attempt": ["attempt"],
        "summary": ["summary"],
        "both": ["review", "attempt"],
        "all": ["review", "attempt", "summary"],
    }.get(mode, ["review"])

    all_urls = urls_for_modes(metas, modes)
    if not all_urls:
        die("no saved page files — re-run: audit.py fetch --quiz <cmid>")

    passes = max(1, int(getattr(args, "retries", 2)) + 1)
    try:
        for i in range(passes):
            current = all_urls if i == 0 else urls_for_modes(retry_metas, modes)
            if not current:
                break
            run_pass(current, f"pass {i + 1}/{passes}")
            per = per_quiz()
            retry_metas = []
            for m in metas:
                if per.get(m["cmid"]):
                    continue
                types = set(m.get("qtypes") or {})
                # An essay-only page is expected to send nothing; don't burn
                # another full browser session on it.
                if types and types <= {"essay"}:
                    continue
                retry_metas.append(m)
            if not retry_metas:
                break
            if i + 1 < passes:
                print(f"  no capture for {len(retry_metas)} quiz(es); retrying")
                time.sleep(1.5)
    finally:
        if started and proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    per = per_quiz()
    print("\ncaptured identity/write requests:")
    for quiz, hits in sorted(per.items()):
        print(f"  {quiz}: {len(hits)} item(s)")
    missing = [m for m in metas if not per.get(m["cmid"])]
    for m in missing:
        types = set(m.get("qtypes") or {})
        if types and types <= {"essay"}:
            print(f"  {m['cmid']}: essay-only, no solution requests expected")
        else:
            print(f"  {m['cmid']}: no capture (settings race, unsupported type, or missing summary table)")
    print(f"capture: {CAPTURE}")

    posts = captured_posts()
    if posts:
        print(f"write payloads: {len(posts)} (run: audit.py export)")
        for post in posts:
            print(f"  {post['endpoint']}: quiz={post['quiz'] or '?'} attempt={post['attempt'] or '?'}")


# --------------------------------------------------------------------------- #
# identities
# --------------------------------------------------------------------------- #


def captured_questions() -> list[dict]:
    """Identity-bearing questions from either transport the client uses.

    Attempt pages send one GET per question (`quiz/solution`). Review pages do
    not: they publish the whole page with `POST quiz/review`, whose body has
    `questions[].identity`. Both paths are valid; the audit must read both.
    """
    rows = []
    if not CAPTURE.exists():
        return rows
    for line in read_text(CAPTURE).splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = row.get("url", "")
        method = (row.get("method") or "GET").upper()
        if "/api/v2/quiz/solution" in url and method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            qid = q.get("questionId", [""])[0]
            if qid:
                rows.append({
                    "quiz": q.get("quizId", [""])[0],
                    "attempt": q.get("attemptId", [""])[0],
                    "id": qid,
                    "identity": q.get("questionIdentity", [""])[0],
                    "type": q.get("questionType", [""])[0],
                })
            continue
        if re.search(r"/api/v2/quiz/(review|attempt)(?:\?|$)", url):
            if not row.get("body"):
                continue
            try:
                body = json.loads(row["body"])
            except json.JSONDecodeError:
                continue
            meta = body.get("quizMeta") or {}
            quiz = str(meta.get("quizId") or "")
            attempt = str(meta.get("attemptId") or "")
            for item in body.get("questions") or []:
                qid = str(item.get("id") or "")
                if not quiz or not qid:
                    continue
                rows.append({
                    "quiz": quiz,
                    "attempt": attempt,
                    "id": qid,
                    "identity": str(item.get("identity") or ""),
                    "type": str(item.get("__type") or ""),
                })
    return rows


def cmd_identities(args) -> None:
    if not CAPTURE.exists():
        die("no capture.jsonl — run: audit.py harness")
    inv = load_json(INVENTORY, {}) or {}
    page_meta = {}
    for mf in PAGES.glob("*/meta.json"):
        m = load_json(mf, {}) or {}
        if m.get("cmid"):
            page_meta[m["cmid"]] = m
    quizzes: dict[str, dict] = {}
    for r in captured_questions():
        quiz = r["quiz"]
        qid = r["id"]
        if not quiz or not qid or not r["identity"]:
            continue
        known = (inv.get("quizzes", {}).get(quiz, {}) or {}) or page_meta.get(quiz, {}) or {}
        item = quizzes.setdefault(quiz, {
            "course_id": known.get("course_id", ""),
            "title": known.get("title", ""),
            "attempt": r["attempt"],
            "questions": {},
        })
        item["questions"][qid] = {
            "questionId": qid,
            "identity": r["identity"],
            "questionType": r["type"],
        }
    out = {
        "generated": now(),
        "quizzes": {
            quiz: {**data, "questions": sorted(data["questions"].values(), key=lambda x: int(x["questionId"]))}
            for quiz, data in quizzes.items()
            if not args.quiz or quiz in args.quiz
        },
    }
    write_json(IDENTITIES, out)
    print(f"wrote {IDENTITIES}")
    for quiz, data in sorted(out["quizzes"].items()):
        types = sorted({q["questionType"] for q in data["questions"]})
        print(f"  {quiz}: {len(data['questions'])} question(s), type={','.join(types)}")


# --------------------------------------------------------------------------- #
# capture -> export -> share (opt-in production writes)
# --------------------------------------------------------------------------- #

ENDPOINTS = ("review", "attempt", "summary", "solve")
WRITE_ENDPOINTS = ("review", "attempt", "summary")


def captured_posts() -> list[dict]:
    """Complete POST bodies the extension sent to the local mock.

    `capture.jsonl` records every HTTPS request that reached the mock, including
    the write payloads. The CLI replays these exact objects; parsing Moodle HTML
    again in Python would risk a payload the server does not expect.
    """
    rows: list[dict] = []
    if not CAPTURE.exists():
        return rows
    seen: set[str] = set()
    for line in read_text(CAPTURE).splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (row.get("method") or "").upper() != "POST":
            continue
        path = urllib.parse.urlparse(row.get("url") or "").path
        endpoint = {
            "/api/v2/quiz/review": "review",
            "/api/v2/quiz/attempt/summary": "summary",
            "/api/v2/quiz/attempt": "attempt",
            "/api/v2/quiz/solve": "solve",
        }.get(path)
        if not endpoint:
            continue
        try:
            body = json.loads(row.get("body") or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        meta = body.get("quizMeta") or {}
        quiz = str(meta.get("quizId") or body.get("quizId") or "")
        attempt = str(meta.get("attemptId") or body.get("attemptId") or "")
        key = sha256_text(canonical_json(body))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "endpoint": endpoint,
            "quiz": quiz,
            "attempt": attempt,
            "sha256": key,
            "captured_at": row.get("ts") or "",
            "body": body,
        })
    return rows


def cmd_export(args) -> None:
    posts = captured_posts()
    wanted = set(args.endpoint or [])
    out: list[dict] = []
    seen: set[str] = set()
    skipped_essay = 0
    for post in posts:
        if args.quiz and post["quiz"] not in args.quiz:
            continue
        if wanted and post["endpoint"] not in wanted:
            continue
        body, dropped = filter_questions(post["body"], args.include_essay)
        skipped_essay += dropped
        if not args.include_essay and post["endpoint"] in ("review", "attempt"):
            questions = body.get("questions")
            if isinstance(questions, list) and not questions:
                print(f"  skip {post['endpoint']} {post['quiz']}: essay-only payload")
                continue
        if args.strip_summary:
            body.pop("reviewSummary", None)
        key = sha256_text(canonical_json(body))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "endpoint": post["endpoint"],
            "quiz": post["quiz"],
            "attempt": post["attempt"],
            "sha256": key,
            "captured_at": post["captured_at"],
            "body": body,
        })
    dest = Path(args.out) if args.out else POSTS
    write_json(dest, {"generated": now(), "posts": out})
    print(f"wrote {dest} ({len(out)} payload(s), {len(posts)} captured)")
    for post in out:
        questions = post["body"].get("questions")
        count = len(questions) if isinstance(questions, list) else "-"
        print(f"  {post['endpoint']}: quiz={post['quiz'] or '?'} "
              f"attempt={post['attempt'] or '?'} questions={count}")
    if skipped_essay:
        print(f"skipped {skipped_essay} essay question(s); pass --include-essay to keep them")


def api_post(url: str, payload: dict, api_key: str = "",
             timeout: int = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "accept": "*/*",
        "content-type": "application/json; charset=utf-8",
        "origin": "https://syncshare.ru",
        "sec-fetch-site": "none",
        "user-agent": UA,
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with api_opener().open(req, timeout=timeout) as r:
            return {
                "http": r.status,
                "request_ref": r.headers.get("X-Request-Ref") or "",
                "body": r.read().decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "http": exc.code,
            "request_ref": exc.headers.get("X-Request-Ref") if exc.headers else "",
            "body": exc.read().decode("utf-8", "replace"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"http": 0, "request_ref": "", "body": "", "error": str(exc)[:200]}


def classify_write(http: int) -> str:
    if 200 <= http < 300:
        return "ok"
    if http in (409, 422):
        return "duplicate"
    if http == 429:
        return "rate_limited"
    if http in (401, 403):
        return "auth"
    return "error" if http else "unknown"


def cmd_share(args) -> None:
    endpoints = args.endpoint or ["review"]
    for endpoint in endpoints:
        if endpoint not in WRITE_ENDPOINTS:
            die(f"share does not implement endpoint {endpoint!r}; "
                f"choose from {', '.join(WRITE_ENDPOINTS)}")

    data = load_json(POSTS, {}) or {}
    posts = data.get("posts") or []
    if not posts:
        die(f"no exported payloads — run: audit.py export (expected {POSTS})")

    inv = load_json(INVENTORY, {}) or {}
    host = args.host or inv.get("host") or "online-edu.mirea.ru"
    base = (args.base_url or API_BASE).rstrip("/") + "/"
    production = base == API_BASE
    if not production and not args.allow_custom_base:
        die("a custom --base-url is for tests only; add --allow-custom-base")
    if production:
        known = {h for h in (inv.get("host"), "online-edu.mirea.ru") if h}
        if host not in known:
            die(f"refusing to write for host {host!r}; "
                f"expected one of {', '.join(sorted(known))}")

    api_key = args.api_key or os.environ.get("SYNCSHARE_API_KEY", "")
    ledger = load_json(SHARED, {}) or {}
    sent = ledger.setdefault("posts", {})

    candidates = []
    for post in posts:
        if post.get("endpoint") not in endpoints:
            continue
        if args.quiz and post.get("quiz") not in args.quiz:
            continue
        candidates.append(post)

    if not candidates:
        print("no matching payloads (check --endpoint / --quiz)")
        return

    if production and not api_key:
        print("warning: no API key; the write endpoints may require one "
              "(set SYNCSHARE_API_KEY or pass --api-key)")

    mode = "send" if args.send else "dry-run"
    print(f"{mode}: {len(candidates)} payload(s), host={host}, base={base}")
    considered = 0
    ok = dup = failed = skipped = 0
    for post in candidates:
        if args.limit and considered >= args.limit:
            break
        key = post.get("sha256") or sha256_text(canonical_json(post["body"]))
        previous = sent.get(key)
        if previous and previous.get("status") in ("ok", "duplicate") and not args.force:
            skipped += 1
            continue
        considered += 1
        endpoint = post["endpoint"]
        questions = post["body"].get("questions")
        count = len(questions) if isinstance(questions, list) else "-"
        label = f"{endpoint} quiz={post.get('quiz') or '?'} attempt={post.get('attempt') or '?'}"
        if not args.send:
            print(f"  [dry-run] {label} questions={count} bytes={len(canonical_json(post['body']))}")
            continue
        url = base + f"v2/quiz/{endpoint}" if endpoint != "summary" else base + "v2/quiz/attempt/summary"
        res = api_post(url, post["body"], api_key, args.timeout)
        status = classify_write(int(res.get("http") or 0))
        sent[key] = {
            "endpoint": endpoint,
            "quiz": post.get("quiz") or "",
            "attempt": post.get("attempt") or "",
            "status": status,
            "http": res.get("http") or 0,
            "request_ref": res.get("request_ref") or "",
            "sent_at": now(),
            "response": (res.get("body") or res.get("error") or "")[:300],
        }
        ledger["generated"] = now()
        write_json(SHARED, ledger)
        if status == "ok":
            ok += 1
        elif status == "duplicate":
            dup += 1
        else:
            failed += 1
        ref = f" ref={res['request_ref']}" if res.get("request_ref") else ""
        print(f"  {label}: HTTP {res.get('http')} -> {status}{ref}")
        if status in ("auth", "rate_limited", "unknown"):
            print(f"  stopping: {status}. Fix auth/rate limit, then re-run; "
                  f"use --force only after checking {SHARED}")
            break
        time.sleep(max(0.0, args.delay))

    print(f"done: ok={ok} duplicate={dup} failed={failed} skipped={skipped} "
          f"ledger={SHARED}")


def cmd_solve(args) -> None:
    data = load_json(POSTS, {}) or {}
    posts = data.get("posts") or []
    if not posts:
        die(f"no exported payloads — run: audit.py export (expected {POSTS})")

    chosen = None
    question = None
    for post in posts:
        if post.get("quiz") != args.quiz:
            continue
        if post.get("endpoint") not in ("review", "attempt"):
            continue
        for item in post["body"].get("questions") or []:
            if str(item.get("id") or "") == str(args.question_id):
                chosen, question = post, item
                break
        if question:
            break
    if not chosen or not question:
        available = sorted({
            str(q.get("id"))
            for post in posts if post.get("quiz") == args.quiz
            for q in (post["body"].get("questions") or [])
            if q.get("id") is not None
        }, key=lambda x: int(x) if x.isdigit() else 0)
        die(f"question {args.question_id} not found for quiz {args.quiz}; "
            f"available: {', '.join(available[:40]) or 'none'}")

    meta = chosen["body"].get("quizMeta") or {}
    payload = {
        "quizMeta": meta,
        "question": question,
        "questionId": question.get("id") or args.question_id,
        "questionIdentity": question.get("identity") or "",
        "webSearch": bool(args.web_search),
        "poll": bool(args.poll),
        "regenerate": bool(args.regenerate),
    }

    base = (args.base_url or API_BASE).rstrip("/") + "/"
    if base != API_BASE and not args.allow_custom_base:
        die("a custom --base-url is for tests only; add --allow-custom-base")
    api_key = args.api_key or os.environ.get("SYNCSHARE_API_KEY", "")
    if not api_key:
        print("warning: no API key; quiz/solve normally requires a signed-in, paid account")

    res = api_post(base + "v2/quiz/solve", payload, api_key, args.timeout)
    for _ in range(max(0, args.poll_attempts - 1)):
        if not args.poll or res.get("http") not in (200, 202):
            break
        if (res.get("body") or "").strip() not in ("", "[]"):
            break
        time.sleep(max(1.0, args.delay))
        payload["poll"] = True
        res = api_post(base + "v2/quiz/solve", payload, api_key, args.timeout)

    out = {
        "generated": now(),
        "quiz": args.quiz,
        "questionId": str(args.question_id),
        "http": res.get("http"),
        "request_ref": res.get("request_ref") or "",
        "response": res.get("body") or res.get("error") or "",
    }
    dest = Path(args.out) if args.out else SOLVE
    write_json(dest, out)
    print(f"wrote {dest} (HTTP {out['http']})")
    try:
        print(json.dumps(json.loads(out["response"]), ensure_ascii=False, indent=2))
    except (json.JSONDecodeError, TypeError):
        print(out["response"])


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


def api_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_solution(params: dict, retries: int = 3, timeout: int = 25) -> list:
    url = API_URL + "?" + urllib.parse.urlencode(params)
    opener = api_opener()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "accept": "*/*",
            "sec-fetch-site": "none",
            "user-agent": UA,
        })
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET solution failed: {last}")


def classify(data) -> dict:
    if not data:
        return {"status": "empty", "suggestions": 0, "submissions": 0,
                "total": 0, "has_correct": False, "confidence": None}
    item = data[0] if isinstance(data, list) else data
    sug = item.get("suggestions") or []
    sub = item.get("submissions") or []
    total = sum(int(s.get("count") or 0) for s in sub)
    if sug and sub:
        status = "suggestions+submissions"
    elif sug:
        status = "suggestions"
    elif sub:
        status = "submissions"
    else:
        status = "anchor-only"
    return {
        "status": status,
        "suggestions": len(sug),
        "submissions": len(sub),
        "total": total,
        "has_correct": any(s.get("correctness") == 2 for s in sug),
        "confidence": max((s.get("confidence") for s in sug), default=None),
    }


def cmd_query(args) -> None:
    ids = load_json(IDENTITIES, {}) or {}
    inv = load_json(INVENTORY, {}) or {}
    if not ids.get("quizzes"):
        die("no identities — run: audit.py identities")
    host = args.host or (inv.get("host") or "online-edu.mirea.ru")
    mid = moodle_id() or args.moodle_id
    if not mid:
        die("cannot determine moodleId — set MOODLE_ID or log in again")

    out = {"generated": now(), "host": host, "moodleId": mid, "quizzes": {}}
    sent = 0
    for quiz, data in sorted(ids["quizzes"].items(), key=lambda kv: int(kv[0])):
        if args.quiz and quiz not in args.quiz:
            continue
        if args.limit and sent >= args.limit:
            break
        course = data.get("course_id") or (inv.get("quizzes", {}).get(quiz, {}) or {}).get("course_id") or args.course
        if not course:
            print(f"  {quiz}: skip, unknown courseId (use --course)")
            continue
        attempt = data.get("attempt") or ""
        rows = []
        for q in data["questions"]:
            if args.limit and sent >= args.limit:
                break
            params = {
                "client": "2.19.0",
                "host": host,
                "courseId": course,
                "quizId": quiz,
                "attemptId": attempt,
                "moodleId": mid,
                "questionIdentity": q["identity"],
                "questionType": q["questionType"],
                "questionId": q["questionId"],
            }
            try:
                payload = api_solution(params)
                res = classify(payload)
            except Exception as exc:  # noqa: BLE001
                res = {"status": "error", "error": str(exc)[:200],
                       "suggestions": 0, "submissions": 0, "total": 0,
                       "has_correct": False, "confidence": None}
            rows.append({**q, **res})
            sent += 1
            time.sleep(args.delay)
        covered = sum(1 for r in rows if r["total"] > 0)
        totals = [r["total"] for r in rows if r["total"] > 0]
        out["quizzes"][quiz] = {
            "course_id": course,
            "title": data.get("title", ""),
            "attempt": attempt,
            "questions": rows,
            "with_data": covered,
            "total_questions": len(rows),
            "submissions_min": min(totals) if totals else 0,
            "submissions_max": max(totals) if totals else 0,
        }
        spread = f"{min(totals)}–{max(totals)}" if totals else "—"
        print(f"  {quiz}: {covered}/{len(rows)} with data, submissions={spread}")
    write_json(RESULTS, out)
    print(f"wrote {RESULTS} ({sent} GET requests)")


def cmd_probe(args) -> None:
    ids = load_json(IDENTITIES, {}) or {}
    inv = load_json(INVENTORY, {}) or {}
    host = args.host or (inv.get("host") or "online-edu.mirea.ru")
    mid = moodle_id() or args.moodle_id
    out = {"generated": now(), "host": host, "probes": []}
    for quiz, data in sorted(ids.get("quizzes", {}).items(), key=lambda kv: int(kv[0])):
        if not data["questions"]:
            continue
        q = data["questions"][0]
        course = data.get("course_id") or args.course or "1"
        base = {
            "client": "2.19.0", "host": host, "courseId": course, "quizId": quiz,
            "attemptId": data.get("attempt") or "1", "moodleId": mid,
            "questionIdentity": q["identity"], "questionType": q["questionType"],
            "questionId": q["questionId"],
        }
        tests = {
            "baseline": dict(base),
            "foreign_host": {**base, "host": "example.com"},
            "wrong_quiz": {**base, "quizId": "1"},
            "wrong_moodle": {**base, "moodleId": "1"},
        }
        row = {"quizId": quiz, "questionId": q["questionId"], "results": {}}
        for name, params in tests.items():
            row["results"][name] = classify(api_solution(params))
            time.sleep(args.delay)
        out["probes"].append(row)
        print(f"  {quiz}: baseline={row['results']['baseline']['status']} "
              f"foreign_host={row['results']['foreign_host']['status']} "
              f"wrong_quiz={row['results']['wrong_quiz']['status']} "
              f"wrong_moodle={row['results']['wrong_moodle']['status']}")
    write_json(PROBE, out)
    print(f"wrote {PROBE}")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def cmd_report(args) -> None:
    inv = load_json(INVENTORY, {}) or {}
    res = load_json(RESULTS, {}) or {}
    quizzes = res.get("quizzes", {})
    if not quizzes:
        die("no results — run: audit.py query")

    lines = [
        "# SyncShare coverage audit",
        "",
        f"- host: `{res.get('host')}`",
        f"- moodleId: `{res.get('moodleId')}`",
        f"- generated: {res.get('generated')}",
        f"- requests: GET `quiz/solution` only (no writes)",
        "",
        "| Дисциплина | Тест | Тип | Вопросов | С данными | Ответов |",
        "|---|---|---|---|---|---|",
    ]
    for quiz, q in sorted(quizzes.items(), key=lambda kv: (kv[1].get("course_id", ""), int(kv[0]))):
        meta = (inv.get("quizzes", {}) or {}).get(quiz, {})
        course = meta.get("course_name") or q.get("course_id") or ""
        title = q.get("title") or meta.get("title") or quiz
        types = sorted({r.get("questionType", "?") for r in q["questions"]}) or ["—"]
        spread = (f"{q['submissions_min']}–{q['submissions_max']}"
                  if q["submissions_max"] else "—")
        lines.append(
            f"| {course} | {title} | {', '.join(types)} | {q['total_questions']} | "
            f"{q['with_data']}/{q['total_questions']} | {spread} |"
        )

    started = []
    for quiz, meta in (inv.get("quizzes", {}) or {}).items():
        for attempt in meta.get("attempts_started", []) or []:
            started.append((quiz, attempt, meta.get("attempts_limit")))
    if started:
        lines += ["", "## Attempts created", ""]
        for quiz, attempt, limit in started:
            warn = " — **1-attempt quiz, will grade 0 on expiry**" if limit == 1 else ""
            lines.append(f"- `{attempt}` in quiz `{quiz}`{warn}")

    if args.full:
        lines += ["", "## Per-question detail", ""]
        for quiz, q in sorted(quizzes.items(), key=lambda kv: int(kv[0])):
            lines += [f"### {quiz} — {q.get('title', '')}", "",
                      "| qid | type | status | suggestions | submissions | total | correct |",
                      "|---|---|---|---|---|---|---|"]
            for r in q["questions"]:
                lines.append(
                    f"| {r['questionId']} | {r.get('questionType', '')} | {r['status']} | "
                    f"{r['suggestions']} | {r['submissions']} | {r['total']} | "
                    f"{'yes' if r['has_correct'] else '—'} |"
                )
            lines.append("")

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def cmd_run(args) -> None:
    if not args.skip_fetch:
        cmd_fetch(args)
    if not args.skip_harness:
        cmd_harness(args)
    if not args.skip_identities:
        cmd_identities(args)
    if not args.skip_query:
        cmd_query(args)
    cmd_report(argparse.Namespace(full=False, out=None))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> None:
    global AUDIT, MIREA, PAGES, VIEWS, HARNESS, EXT, CAPTURE, INVENTORY, IDENTITIES, RESULTS, PROBE
    global POSTS, SHARED, SOLVE, CAPTURES
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audit-dir", help="state directory (default: $SYNC_AUDIT_DIR or ~/.syncshare-audit)")
    ap.add_argument("--mirea-dir", help="mirea-moodle state directory (default: $MIREA_DIR or ~/.mirea)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inventory", help="discover quizzes")
    p.add_argument("--course", action="append", help="course id (repeatable); default: all")
    p.add_argument("--all", action="store_true", help="all courses (the default)")
    p.add_argument("--refresh", action="store_true", help="re-fetch cached pages")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("fetch", help="download attempt/review pages")
    p.add_argument("--quiz", action="append", required=True, help="quiz cmid (repeatable)")
    p.add_argument("--course", help="course id fallback for identity metadata")
    p.add_argument("--allow-single-attempt", action="store_true",
                   help="allow starting a quiz that has only one attempt")
    p.add_argument("--new-attempt", action="store_true",
                   help="start a new attempt even if a review/in-progress one exists")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("harness", help="run the extension against saved pages")
    p.add_argument("--quiz", action="append", help="limit to quiz cmid (repeatable)")
    p.add_argument("--mode", choices=["review", "attempt", "summary", "both", "all"],
                   default="review",
                   help="which request path(s) to exercise (default: review)")
    p.add_argument("--retries", type=int, default=2,
                   help="extra browser sessions for pages with no capture (default 2)")
    p.set_defaults(func=cmd_harness)

    p = sub.add_parser("identities", help="extract identities from capture.jsonl")
    p.add_argument("--quiz", action="append", help="limit to quiz cmid (repeatable)")
    p.set_defaults(func=cmd_identities)

    p = sub.add_parser("export", help="extract complete write payloads from capture.jsonl")
    p.add_argument("--out", help=f"output path (default: {POSTS})")
    p.add_argument("--quiz", action="append", help="limit to quiz cmid (repeatable)")
    p.add_argument("--endpoint", action="append", choices=list(ENDPOINTS),
                   help="limit to endpoint (repeatable)")
    p.add_argument("--include-essay", action="store_true",
                   help="keep essay questions (their text may be personal)")
    p.add_argument("--strip-summary", action="store_true",
                   help="drop reviewSummary before export")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("share", help="replay exported payloads to production (opt-in)")
    p.add_argument("--endpoint", action="append", choices=list(WRITE_ENDPOINTS),
                   help="review/attempt/summary (repeatable, default: review)")
    p.add_argument("--quiz", action="append", help="limit to quiz cmid (repeatable)")
    p.add_argument("--limit", type=int, default=0, help="max payloads to consider (0 = all)")
    p.add_argument("--delay", type=float, default=2.0,
                   help="seconds between writes (default 2.0; do not lower for production)")
    p.add_argument("--send", action="store_true",
                   help="actually POST; without it the command is a dry-run")
    p.add_argument("--force", action="store_true",
                   help="re-send payloads recorded as ok/duplicate in the ledger")
    p.add_argument("--api-key", default=os.environ.get("SYNCSHARE_API_KEY", ""),
                   help="SyncShare X-Api-Key (or SYNCSHARE_API_KEY)")
    p.add_argument("--host", help="Moodle host (default from inventory)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--base-url", default=API_BASE, help=argparse.SUPPRESS)
    p.add_argument("--allow-custom-base", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_share)

    p = sub.add_parser("solve", help="ask quiz/solve for one captured question (opt-in)")
    p.add_argument("--quiz", required=True, help="quiz cmid")
    p.add_argument("--question-id", required=True, help="Moodle question id from posts.json")
    p.add_argument("--web-search", action="store_true")
    p.add_argument("--poll", action="store_true", help="poll while the answer is generated")
    p.add_argument("--poll-attempts", type=int, default=10)
    p.add_argument("--regenerate", action="store_true")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--api-key", default=os.environ.get("SYNCSHARE_API_KEY", ""),
                   help="SyncShare X-Api-Key (or SYNCSHARE_API_KEY)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--out", help=f"output path (default: {SOLVE})")
    p.add_argument("--base-url", default=API_BASE, help=argparse.SUPPRESS)
    p.add_argument("--allow-custom-base", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_solve)

    p = sub.add_parser("query", help="query quiz/solution (GET only)")
    p.add_argument("--quiz", action="append", help="limit to quiz cmid (repeatable)")
    p.add_argument("--host", help="Moodle host (default from inventory)")
    p.add_argument("--course", help="fallback courseId")
    p.add_argument("--moodle-id", help="fallback moodleId")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--limit", type=int, default=0, help="max requests (0 = all)")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("probe", help="verify that host is the lookup key")
    p.add_argument("--host", help="Moodle host")
    p.add_argument("--course", help="fallback courseId")
    p.add_argument("--moodle-id", help="fallback moodleId")
    p.add_argument("--delay", type=float, default=1.0)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("report", help="render a coverage table")
    p.add_argument("--full", action="store_true", help="include per-question detail")
    p.add_argument("--out", help="write markdown to this path")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="fetch + harness + identities + query + report")
    p.add_argument("--quiz", action="append", required=True, help="quiz cmid (repeatable)")
    p.add_argument("--course", help="fallback courseId")
    p.add_argument("--allow-single-attempt", action="store_true")
    p.add_argument("--new-attempt", action="store_true")
    p.add_argument("--host", help="Moodle host")
    p.add_argument("--moodle-id", help="fallback moodleId")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--mode", choices=["review", "attempt", "summary", "both", "all"],
                   default="review",
                   help="harness request path(s); write endpoints are never sent by run")
    p.add_argument("--skip-fetch", action="store_true")
    p.add_argument("--skip-harness", action="store_true")
    p.add_argument("--skip-identities", action="store_true")
    p.add_argument("--skip-query", action="store_true")
    p.add_argument("--retries", type=int, default=2,
                   help="extra browser sessions for pages with no capture (default 2)")
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    if args.audit_dir:
        os.environ["SYNC_AUDIT_DIR"] = args.audit_dir
    if args.mirea_dir:
        os.environ["MIREA_DIR"] = args.mirea_dir

    AUDIT = Path(os.environ.get("SYNC_AUDIT_DIR", str(Path.home() / ".syncshare-audit")))
    MIREA = Path(os.environ.get("MIREA_DIR", str(Path.home() / ".mirea")))
    PAGES = AUDIT / "pages"
    VIEWS = AUDIT / "view"
    HARNESS = AUDIT / "harness"
    EXT = AUDIT / "extension"
    CAPTURE = AUDIT / "capture.jsonl"
    INVENTORY = AUDIT / "inventory.json"
    IDENTITIES = AUDIT / "identities.json"
    RESULTS = AUDIT / "results.json"
    PROBE = AUDIT / "probe.json"
    POSTS = AUDIT / "posts.json"
    SHARED = AUDIT / "shared.json"
    SOLVE = AUDIT / "solve.json"
    CAPTURES = AUDIT / "captures"

    ensure_dirs()
    args.func(args)


if __name__ == "__main__":
    main()
