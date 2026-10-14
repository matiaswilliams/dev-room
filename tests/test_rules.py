#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""The rules the board enforces, and the requests it refuses.

Runs an in-process board on a free port and a throwaway git origin.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="dev-room-rules-")
os.environ["DEV_ROOM_DATA"] = os.path.join(WORK, "board.json")
os.environ.pop("DEV_ROOM_URL", None)
os.environ.pop("AGENT_BOARD_URL", None)

import mcp_server  # noqa: E402
import server  # noqa: E402

server._BOARD = None
fails = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL ") + name + ("" if cond or not detail else " " + str(detail)))
    if not cond:
        fails.append(name)


def http(method, base, path, payload=None, headers=None, raw=None):
    data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    req = urllib.request.Request(base + path, data=data, method=method)
    if payload is not None and raw is None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read().decode()
            return res.status, (json.loads(body) if body else {}), dict(res.headers)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"error": text}
        return exc.code, parsed, dict(exc.headers)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_checkout() -> str:
    """A checkout whose origin is a local bare repository."""
    origin = os.path.join(WORK, "origin.git")
    checkout = os.path.join(WORK, "checkout")
    git(WORK, "init", "--bare", "-q", origin)
    git(WORK, "clone", "-q", origin, checkout)
    git(checkout, "config", "user.email", "test@example.com")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "checkout", "-q", "-b", "feature/page")
    with open(os.path.join(checkout, "page.txt"), "w", encoding="utf-8") as handle:
        handle.write("one\n")
    git(checkout, "add", "page.txt")
    git(checkout, "commit", "-q", "-m", "one")
    return checkout


def main():
    httpd = server.BoardServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    for name, role in [("Ora", "orchestrator"), ("Ivy", "implementer"), ("Cy", "checker"), ("Hu", "human")]:
        code, row, _ = http("POST", base, "/api/join", {"name": name, "role": role, "model": "test"})
        check(f"join {name}", code == 201, row)

    # Requests from outside this machine.
    code, body, _ = http("POST", base, "/api/feed", raw=json.dumps({"by": "ora", "note": "plain"}).encode(),
                         headers={"Content-Type": "text/plain"})
    check("text/plain post is refused", code == 415, body)
    code, body, _ = http("POST", base, "/api/feed", {"by": "ora", "note": "from a web page"},
                         headers={"Origin": "https://evil.example"})
    check("foreign origin is refused", code == 403, body)
    code, body, _ = http("POST", base, "/api/feed", {"by": "ora", "note": "sandboxed"}, headers={"Origin": "null"})
    check("null origin is refused", code == 403, body)
    code, body, _ = http("GET", base, "/api/state", headers={"Host": "attacker.example:8791"})
    check("foreign host is refused", code == 403, body)
    code, body, _ = http("POST", base, "/api/feed", {"by": "ora", "note": "from the dashboard"},
                         headers={"Origin": base})
    check("local origin is accepted", code == 201, body)
    code, body, _ = http("POST", base, "/api/feed", raw=b"[1, 2]", headers={"Content-Type": "application/json"})
    check("a JSON list body is refused", code == 400, body)
    code, body, _ = http("POST", base, "/api/agents/cy/remove", {})
    check("remove needs by", code == 400, body)
    code, body, _ = http("POST", base, "/api/agents/cy/remove", {"by": "ivy"})
    check("an implementer cannot remove another agent", code == 403, body)
    code, body, _ = http("POST", base, "/api/agents/ivy/remove", {"by": "hu"})
    code2, body2, _ = http("POST", base, "/api/agents/ivy/restore", {"by": "ora"})
    check("the human removes and an orchestrator restores", code == 200 and code2 == 200 and body2["active"], body2)

    # State polling answers 304 when nothing changed.
    code, _, headers = http("GET", base, "/api/state")
    tag = headers.get("ETag", "")
    code2, _, _ = http("GET", base, "/api/state", headers={"If-None-Match": tag})
    check("unchanged state answers 304", code == 200 and tag and code2 == 304, (code, tag, code2))
    http("POST", base, "/api/feed", {"by": "ora", "note": "something changed"})
    code3, _, _ = http("GET", base, "/api/state", headers={"If-None-Match": tag})
    check("changed state answers 200", code3 == 200, code3)
    code, _, _ = http("POST", base, "/api/heartbeat", {"by": "hu"})
    code4, _, headers4 = http("GET", base, "/api/state")
    code5, _, _ = http("GET", base, "/api/state", headers={"If-None-Match": headers4.get("ETag", "")})
    check("a heartbeat does not change the tag", code == 200 and code5 == 304, (code, code5))

    # Review rules on a task that is not code.
    code, task, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Review the copy", "brief": "Read it", "owner": "cy", "checker": "ivy", "code": False,
    })
    check("open a review task", code == 201, task)
    tid = task["id"]
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "cy", "status": "done"})
    check("a review needs the sha it read", code == 400 and "sha" in body.get("error", ""), body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "cy", "status": "done", "sha": "abcdef1"})
    check("review done with a sha", code == 200 and body["status"] == "done", body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "cy", "status": "approved"})
    check("the owner cannot approve their own work", code == 403, body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "ivy", "status": "rejected"})
    check("a rejection needs corrections", code == 400, body)
    fix = [{"id": "L1", "severity": "major", "file": "copy.md:3", "what": "Typo."}]
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "ivy", "status": "rejected", "corrections": fix})
    check("first rejection", code == 200 and body["status"] == "rejected" and body["checklist"][0]["id"] == "L1", body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "cy", "status": "done", "sha": "abcdef2"})
    check("done is blocked while the checklist is open", code == 400 and "L1" in body.get("error", ""), body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status",
                         {"by": "cy", "status": "done", "sha": "abcdef2", "checked": ["L1"]})
    check("done after checking L1", code == 200 and body["status"] == "done", body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "ivy", "status": "rejected", "corrections": fix})
    check("a second rejection stops the task", code == 200 and body["status"] == "stopped", body)
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/approach", {"by": "cy", "text": "try again"})
    check("a stopped task is closed", code == 409, body)

    # Code tasks: one branch per implementer, and done means pushed.
    checkout = make_checkout()
    code, task, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Write the page", "owner": "ivy", "checker": "cy",
        "branch": "feature/page", "repo": checkout,
    })
    check("open a code task", code == 201 and task["code"] is True, task)
    tid = task["id"]
    code, body, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Other work", "owner": "ivy", "checker": "cy",
        "branch": "feature/other", "repo": checkout,
    })
    check("a second live branch is refused", code == 409, body)
    code, body, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "No repo", "owner": "ivy", "checker": "cy", "branch": "feature/page", "repo": WORK,
    })
    check("a code task needs a git checkout", code == 400 and "git" in body.get("error", ""), body)
    sha = git_out(checkout, "rev-parse", "HEAD")
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "ivy", "status": "done", "sha": sha[:10]})
    check("done is refused before the push", code == 400 and "origin" in body.get("error", ""), body)
    git(checkout, "push", "-q", "origin", "feature/page")
    code, body, _ = http("POST", base, f"/api/tasks/{tid}/status", {"by": "ivy", "status": "done", "sha": sha[:10]})
    check("done after the push stores the full sha", code == 200 and body.get("head") == sha, body)
    code, feed, _ = http("GET", base, "/api/feed?for=cy")
    duties = [(row["id"], row["duty"]) for row in feed.get("tasks") or []]
    check("the checker gets the check duty", (tid, "check") in duties, duties)

    # A working task with no log for two hours stops, and goes back to its owner.
    code, task, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Idle work", "owner": "cy", "checker": "ivy", "code": False,
    })
    idle = task["id"]
    http("POST", base, f"/api/tasks/{idle}/approach", {"by": "cy", "text": "Starting."})
    with server.LOCK:
        data = server.load()
        row = next(item for item in data["tasks"] if item["id"] == idle)
        old = (datetime.now(UTC) - timedelta(minutes=121)).isoformat(timespec="seconds")
        for entry in row["log"]:
            entry["at"] = old
        row["created_at"] = row["updated_at"] = old
        stopped = server.release_stale(data)
    check("an idle task is stopped", stopped and row["status"] == "stopped" and row.get("stop_kind") == "idle", row)
    with server.LOCK:
        returned = server.handed_back(server.load(), "cy")
    check("the idle task goes back to its owner", idle in returned, returned)

    # Branch is a field. Text is read only when the task has no branch key.
    old = {
        "id": 910, "title": "Ship branch feature/old", "brief": "See branch feature/old",
        "owner": "ivy", "checker": "cy", "status": "working",
        "created_at": server.now(), "updated_at": server.now(), "log": [],
        "repo": checkout, "code": True, "checklist": [],
    }
    check("an old task reads its branch from the text", server.task_branch(old) == "feature/old", server.task_branch(old))
    ready, err, _, _, branch_name, _ = server.Handler._done_ready(None, server.load(), old, {"sha": "abc1234"})
    check("done on an old task uses that branch", ready and branch_name == "feature/old", (ready, err, branch_name))
    stored = dict(old, branch="", title="Ship branch feature/sneak", brief="")
    ready, err, _, _, branch_name, _ = server.Handler._done_ready(None, server.load(), stored, {"sha": "abc1234"})
    check("a stored branch is not filled in from the text", ready and branch_name == "", (ready, err, branch_name))
    with server.LOCK:
        data = server.load()
        data["tasks"].append(old)
        data["next_task_id"] = max(data["next_task_id"], 911)
        server.save(data)
    code, feed, _ = http("GET", base, "/api/feed?for=ivy")
    queued = next((row for row in feed.get("tasks") or [] if row["id"] == 910), {})
    check("the queue shows the branch read from an old task", queued.get("branch") == "feature/old", queued.get("branch"))
    code, body, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Write branch feature/sneak", "owner": "ivy", "checker": "cy",
        "branch": "feature/new", "repo": checkout,
    })
    check("an old task still blocks a different branch", code == 409 and "feature/old" in body.get("error", ""), body)
    code, body, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Write branch feature/sneak", "owner": "ivy", "checker": "cy",
        "repo": checkout,
    })
    check("a new task does not take a branch from its title", code == 400 and "needs a branch" in body.get("error", ""), body)

    # Saying "finished" does not end an idle stop. The finished flag does.
    code, task, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Said finished", "owner": "cy", "checker": "ivy", "code": False,
    })
    said = task["id"]
    http("POST", base, f"/api/tasks/{said}/approach", {"by": "cy", "text": "Starting."})
    code, body, _ = http("POST", base, f"/api/tasks/{said}/note", {"by": "cy", "note": "This work is finished and merged."})
    check("a note without the flag stores no finished field", code == 200 and "finished" not in body["log"][-1], body["log"][-1])
    code, task, _ = http("POST", base, "/api/tasks", {
        "by": "ora", "title": "Marked finished", "owner": "cy", "checker": "ivy", "code": False,
    })
    marked = task["id"]
    code, body, _ = http("POST", base, f"/api/tasks/{marked}/approach", {
        "by": "cy", "text": "Still writing the last line.", "finished": True,
    })
    check("a finish flag is stored on the approach", code == 200 and body["log"][-1].get("finished") is True, body["log"][-1])
    with server.LOCK:
        data = server.load()
        old_at = (datetime.now(UTC) - timedelta(minutes=121)).isoformat(timespec="seconds")
        for item in data["tasks"]:
            if item["id"] in {said, marked}:
                for entry in item["log"]:
                    entry["at"] = old_at
                item["created_at"] = item["updated_at"] = old_at
        server.release_stale(data)
        # The earlier idle stop shares this second. Push it back so the prose finish is the newest.
        idle_row = next(item for item in data["tasks"] if item["id"] == idle)
        older = (datetime.now(UTC) - timedelta(minutes=180)).isoformat(timespec="seconds")
        for entry in idle_row["log"]:
            entry["at"] = older
        idle_row["created_at"] = idle_row["updated_at"] = older
        returned = server.handed_back(data, "cy")
    check("saying finished still hands the task back", said in returned, returned)
    check("finished true keeps the idle stop off the queue", marked not in returned, returned)
    prompt = server.join_prompt("http://127.0.0.1:9", server.load())
    check("the prompt asks for the branch field and the finish flag",
          "does not read a branch name" in prompt and "finished to true" in prompt
          and "last note is not a finish" not in prompt, "")

    # A feed read without since is capped, so a first read stays small.
    with server.LOCK:
        data = server.load()
        for n in range(server.FEED_LIMIT + 20):
            data["feed"].append({"id": data["next_feed_id"], "at": server.now(), "by": "ora", "to": None,
                                 "mentions": [], "to_all": False, "note": f"filler {n}"})
            data["next_feed_id"] += 1
        server.save(data)
        total = sum(1 for post in data["feed"] if server.feed_visible(post, "cy"))
    code, feed, _ = http("GET", base, "/api/feed?for=cy")
    check("a first feed read is capped", code == 200 and len(feed["feed"]) == server.FEED_LIMIT
          and feed["omitted"] == total - server.FEED_LIMIT and feed["feed"][-1]["note"].startswith("filler"),
          (len(feed["feed"]), feed.get("omitted")))
    code, feed, _ = http("GET", base, "/api/feed?for=cy&limit=5")
    check("limit picks the newest posts", code == 200 and [p["note"] for p in feed["feed"]][-1] == f"filler {server.FEED_LIMIT + 19}"
          and len(feed["feed"]) == 5, len(feed.get("feed") or []))
    code, feed, _ = http("GET", base, "/api/feed?for=cy&since=2000-01-01T00:00:00+00:00")
    check("since is not capped", code == 200 and len(feed["feed"]) == total and feed["omitted"] == 0, len(feed["feed"]))
    mcp_feed = mcp_server.call_tool("feed", {"agent": "cy", "limit": 3})
    check("mcp feed takes a limit", len(mcp_feed["feed"]) == 3, len(mcp_feed["feed"]))

    # Old closed tasks and old posts move to board-archive/, the rest stays.
    with server.LOCK:
        data = server.load()
        approved = {"id": 900, "title": "Old approved", "owner": "ivy", "checker": "cy", "status": "approved",
                    "created_at": "2026-01-02T10:00:00+00:00", "updated_at": "2026-01-02T10:00:00+00:00",
                    "log": [{"at": "2026-01-02T10:00:00+00:00", "by": "cy", "kind": "status",
                             "status": "approved", "text": ""}], "checklist": []}
        held = {"id": 901, "title": "Old stopped with open items", "owner": "ivy", "checker": "cy",
                "status": "stopped", "created_at": "2026-01-02T10:00:00+00:00",
                "updated_at": "2026-01-02T10:00:00+00:00",
                "log": [{"at": "2026-01-02T10:00:00+00:00", "by": "cy", "kind": "status",
                         "status": "stopped", "text": ""}],
                "checklist": [{"id": "L1", "severity": "major", "file": "a", "what": "b", "done": False}]}
        old_post = {"id": 9000, "at": "2026-01-03T10:00:00+00:00", "by": "ora", "to": None,
                    "mentions": [], "to_all": False, "note": "an old post"}
        data["tasks"] += [approved, held]
        data["feed"].insert(0, old_post)
        live_ids = {task["id"] for task in data["tasks"] if task["id"] not in (900, 901)}
        moved = server.archive_old(data)
        server.save(data)
        kept = {task["id"] for task in data["tasks"]}
    path = os.path.join(server.archive_dir(), "2026-01.jsonl")
    rows = [json.loads(line) for line in open(path, encoding="utf-8")] if os.path.exists(path) else []
    check("archive moves the old approved task and the old post",
          moved == 2 and {(r["kind"], r["item"]["id"]) for r in rows} == {("task", 900), ("post", 9000)}, rows)
    check("archive keeps recent tasks and a task handed back to its owner",
          live_ids <= kept and 901 in kept and 900 not in kept, kept)
    with server.LOCK:
        again = server.archive_old(server.load())
        off = server.archive_old(server.load(), days=0)
    check("a second archive pass moves nothing, and days=0 is off", again == 0 and off == 0, (again, off))

    # MCP answers with the protocol version the client asked for.
    hello = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}})
    check("mcp echoes a supported version", hello["result"]["protocolVersion"] == "2025-06-18", hello)
    hello = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "1999-01-01"}})
    check("mcp falls back to its newest version",
          hello["result"]["protocolVersion"] == mcp_server.SUPPORTED_VERSIONS[0], hello)
    update = next(tool for tool in mcp_server.TOOLS if tool["name"] == "task_update")
    check("mcp task_update accepts finished", "finished" in update["inputSchema"]["properties"], update["inputSchema"])

    version_dir = tempfile.mkdtemp(prefix="dev-room-version-")
    version_file = os.path.join(version_dir, "board.json")
    cli_env = os.environ.copy()
    cli_env["DEV_ROOM_DATA"] = version_file
    cli_env.pop("DEV_ROOM_URL", None)
    cli_env.pop("AGENT_BOARD_URL", None)
    printed = subprocess.run(
        [sys.executable, os.path.join(ROOT, "server.py"), "--version"],
        capture_output=True, text=True, env=cli_env, timeout=5,
    )
    check("version prints and does not create a board",
          printed.returncode == 0
          and printed.stdout == f"dev-room {server.VERSION}\n"
          and not os.path.exists(version_file),
          (printed.returncode, printed.stdout, printed.stderr))
    refused = subprocess.run(
        [sys.executable, os.path.join(ROOT, "server.py"), "--host", "0.0.0.0"],
        capture_output=True, text=True, env=cli_env, timeout=5,
    )
    check("a public host is refused before the board listens",
          refused.returncode != 0
          and "no accounts" in refused.stderr
          and not os.path.exists(version_file),
          (refused.returncode, refused.stderr, refused.stdout))
    check("the default commands stay on localhost",
          server.serve_from([]) == ("127.0.0.1", 8791)
          and server.serve_from(["8792"]) == ("127.0.0.1", 8792)
          and server.serve_from(["--host", "localhost", "8792"]) == ("127.0.0.1", 8792))
    shutil.rmtree(version_dir, ignore_errors=True)

    scratch_archive = tempfile.mkdtemp(prefix="dev-room-archive-")
    with open(os.path.join(scratch_archive, "2026-01.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "task", "item": {"id": 7, "title": "Write the lantern page"},
        }) + "\n")
        handle.write(json.dumps({
            "kind": "post",
            "item": {"id": 3, "note": "the lantern page is approved"},
        }) + "\n")
    script = os.path.join(ROOT, "scripts", "search_archive.py")
    found = subprocess.run(
        [sys.executable, script, "lantern", "--dir", scratch_archive],
        capture_output=True, text=True, timeout=5,
    )
    missed = subprocess.run(
        [sys.executable, script, "absent", "--dir", scratch_archive],
        capture_output=True, text=True, timeout=5,
    )
    check("archive search returns kind and id",
          found.returncode == 0 and found.stdout.splitlines() == ["task 7", "post 3"],
          found.stdout)
    check("archive search miss prints no rows",
          missed.returncode == 0 and missed.stdout == "", missed.stdout)
    shutil.rmtree(scratch_archive, ignore_errors=True)

    httpd.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    if fails:
        print(f"{len(fails)} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
