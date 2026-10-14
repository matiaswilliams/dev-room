#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Empty-board HTTP loop and MCP tools against that same board.

Run twice. Each run uses a new data file and must pass.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA_DIR = tempfile.mkdtemp(prefix="agent-board-public-")
os.environ["DEV_ROOM_DATA"] = os.path.join(DATA_DIR, "board.json")
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


def http(method, base, path, payload=None):
    raw = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=raw, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read()
            if path.startswith("/api/"):
                return res.status, json.loads(body.decode())
            return res.status, body.decode()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"error": text}
        return exc.code, parsed


def mcp_round(proc, message):
    """One request and its reply, one JSON object per line, as MCP stdio clients send them."""
    proc.stdin.write(json.dumps(message).encode() + b"\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("mcp server closed stdout")
    return json.loads(line.decode())


def boot_server(number: int):
    """Start server.py on a fresh data file and require an empty roster."""
    work = tempfile.mkdtemp(prefix=f"agent-board-boot-{number}-")
    data_path = os.path.join(work, "board.json")
    port = 8870 + number
    env = os.environ.copy()
    env["DEV_ROOM_DATA"] = data_path
    env.pop("DEV_ROOM_URL", None)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = threading.Event()
        ready = False
        for _ in range(50):
            try:
                code, state = http("GET", f"http://127.0.0.1:{port}", "/api/state")
            except Exception:
                code, state = 0, {}
            if code == 200:
                ready = True
                break
            if proc.poll() is not None:
                break
            threading.Event().wait(0.1)
        code, prompt = http("GET", f"http://127.0.0.1:{port}", "/prompt") if ready else (0, "")
        empty = (
            ready
            and state.get("agents") == []
            and state.get("tasks") == []
            and "nobody yet" in prompt
            and "No open, working, rejected, or done tasks." in prompt
        )
        check(f"boot {number} is an empty board", empty, {"code": code, "agents": state.get("agents"), "tasks": state.get("tasks")})
        scratch = os.environ.get("DEV_ROOM_SCRATCH")
        if scratch and empty:
            os.makedirs(scratch, exist_ok=True)
            with open(os.path.join(scratch, f"boot-{number}.txt"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"agents": state["agents"], "tasks": state["tasks"]}, indent=2))
                handle.write("\n\n")
                handle.write(prompt)
        deadline.set()
    finally:
        proc.kill()
        proc.wait(timeout=5)
        shutil.rmtree(work, ignore_errors=True)


def main():
    data_path = os.environ["DEV_ROOM_DATA"]
    check("fresh file is absent", not os.path.exists(data_path))
    fresh = server.load()
    check("fresh roster is empty", fresh["agents"] == [] and fresh["tasks"] == [] and fresh["feed"] == [])

    httpd = server.BoardServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    code, state = http("GET", base, "/api/state")
    check("http state is empty", code == 200 and state["agents"] == [] and state["tasks"] == [], state if code != 200 else "")

    icon_req = urllib.request.Request(base + "/favicon.ico")
    with urllib.request.urlopen(icon_req, timeout=10) as icon_res:
        icon = icon_res.read()
        icon_type = icon_res.headers.get("Content-Type", "")
        icon_status = icon_res.status
    check("favicon is an icon",
          icon_status == 200 and icon_type.startswith("image/") and icon[:4] == b"\x00\x00\x01\x00",
          (icon_status, icon_type, icon[:4]))

    code, joined = http("POST", base, "/api/join", {
        "name": "Ada", "role": "implementer", "model": "test",
    })
    check("http join", code == 201 and joined.get("id") == "ada", joined)
    code, posted = http("POST", base, "/api/feed", {"by": "ada", "note": "HTTP note from Ada."})
    check("http note", code == 201 and posted.get("note") == "HTTP note from Ada.", posted)
    code, feed = http("GET", base, "/api/feed?for=ada")
    notes = [post.get("note") for post in feed.get("feed") or []]
    check("http feed returns the note", code == 200 and "HTTP note from Ada." in notes, notes)
    check("http feed returns a task queue", code == 200 and feed.get("tasks") == [], feed.get("tasks"))

    code, checker = http("POST", base, "/api/join", {
        "name": "Bea", "role": "checker", "model": "test",
    })
    check("http checker", code == 201, checker)
    code, task = http("POST", base, "/api/tasks", {
        "by": "ada", "title": "Write the page", "brief": "A short page",
        "owner": "ada", "checker": "bea", "branch": "feature/page", "repo": DATA_DIR,
    })
    # An implementer task with code true requires a real git checkout. This task is not code.
    if code != 201:
        code, task = http("POST", base, "/api/tasks", {
            "by": "bea", "title": "Write the page", "brief": "A short page",
            "owner": "bea", "checker": "ada",
        })
    check("http task", code == 201 and task.get("id") == 1, task)

    mcp_join = mcp_server.call_tool("join", {"name": "Cam", "role": "orchestrator", "model": "test"})
    print("mcp join return", json.dumps(mcp_join))
    check("mcp join", mcp_join.get("id") == "cam", mcp_join)
    mcp_note = mcp_server.call_tool("note", {"by": "cam", "note": "MCP note from Cam."})
    print("mcp note return", json.dumps(mcp_note))
    check("mcp note", mcp_note.get("note") == "MCP note from Cam.", mcp_note)
    updated = mcp_server.call_tool("task_update", {
        "by": "ada" if task.get("owner") == "ada" else "bea",
        "id": str(task["id"]),
        "action": "note",
        "text": "MCP updated the task.",
    })
    log_text = [entry.get("text") for entry in updated.get("log") or []]
    print("mcp task return", json.dumps({"id": updated.get("id"), "log": log_text}))
    check("mcp task update", "MCP updated the task." in log_text, updated)

    code, seen = http("GET", base, "/api/feed?for=cam")
    seen_notes = [post.get("note") for post in seen.get("feed") or []]
    check("http sees the mcp note", code == 200 and "MCP note from Cam." in seen_notes, seen_notes)
    code, after = http("GET", base, "/api/state")
    stored = next(item for item in after["tasks"] if item["id"] == task["id"])
    stored_text = [entry.get("text") for entry in stored.get("log") or []]
    check("http sees the mcp task update", "MCP updated the task." in stored_text, stored_text)
    mcp_feed = mcp_server.call_tool("feed", {"agent": "cam"})
    check("mcp feed sees its note", any(post.get("note") == "MCP note from Cam." for post in mcp_feed.get("feed") or []))
    check("mcp feed has a task queue", isinstance(mcp_feed.get("tasks"), list))

    launch_error = ""
    try:
        env = os.environ.copy()
        env["DEV_ROOM_URL"] = base
        env["DEV_ROOM_DATA"] = data_path
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "mcp_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        hello = mcp_round(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        })
        check("mcp launcher initializes", "result" in hello, hello)
        listed = mcp_round(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        check("mcp launcher lists tools", names == ["join", "feed", "note", "task_update"], names)
        called = mcp_round(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "note", "arguments": {"by": "cam", "note": "Launcher note from Cam."}},
        })
        text = called["result"]["content"][0]["text"]
        check("mcp launcher posts", "Launcher note from Cam." in text and not called["result"].get("isError"), text)
        proc.kill()
        code, final = http("GET", base, "/api/feed?for=cam")
        final_notes = [post.get("note") for post in final.get("feed") or []]
        check("http sees the launcher note", "Launcher note from Cam." in final_notes, final_notes)
    except Exception as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        print("mcp launcher failed", launch_error)
    scratch = os.environ.get("DEV_ROOM_SCRATCH")
    if scratch and launch_error:
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, "mcp-launch-error.log"), "w", encoding="utf-8") as handle:
            handle.write(launch_error + "\n")

    httpd.shutdown()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    boot_server(1)
    boot_server(2)
    if fails:
        print(f"{len(fails)} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
