#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""MCP stdio server for the local agent board.

The tools call the same operations as the HTTP routes. When DEV_ROOM_URL is
set, this process forwards to that running board so it does not keep a second
copy of the data. Without that variable, the tools read and write the data
file themselves (DEV_ROOM_DATA, or board.json next to server.py). The older
AGENT_BOARD_URL and AGENT_BOARD_DATA names still work.

Messages on stdio are one JSON object per line, as the MCP stdio transport
requires. A Content-Length framed message is still read, for old test clients.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import server

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def board_url() -> str:
    return os.environ.get("DEV_ROOM_URL") or os.environ.get("AGENT_BOARD_URL") or ""

TOOLS = [
    {
        "name": "join",
        "description": "Join the local board. Returns your agent id. Send that id as by on later calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string", "enum": ["orchestrator", "implementer", "checker", "human"]},
                "model": {"type": "string"},
            },
            "required": ["name", "role", "model"],
        },
    },
    {
        "name": "feed",
        "description": "Read the feed posts you can see and the tasks on your queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Your agent id or name."},
                "since": {"type": "string", "description": "Optional ISO-8601. Returns posts after this time."},
                "limit": {"type": "integer", "description": "Without since, how many of the newest posts to return. Default 50, at most 500."},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "note",
        "description": "Post a feed note of at most 600 characters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "note": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["by", "note"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a task. action is note, approach, check, or status. On a note or an approach, finished true marks that entry as the finish.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "id": {"type": "string", "description": "Task id."},
                "action": {"type": "string", "enum": ["note", "approach", "check", "status"]},
                "text": {"type": "string"},
                "note": {"type": "string"},
                "status": {"type": "string"},
                "sha": {"type": "string"},
                "branch": {"type": "string"},
                "repo": {"type": "string"},
                "finished": {"type": "boolean"},
            },
            "required": ["by", "id", "action"],
        },
    },
]


class BoardToolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _direct(name: str, arguments: dict) -> tuple[int, object]:
    if name == "join":
        return server.board_call("join", arguments)
    if name == "feed":
        return server.board_call("feed", {
            "for": arguments.get("agent") or arguments.get("for") or "",
            "since": arguments.get("since") or "",
            "limit": arguments.get("limit") or "",
        })
    if name == "note":
        return server.board_call("note", arguments)
    if name == "task_update":
        return server.board_call("task", arguments)
    return 404, {"error": f"unknown tool {name}"}


def _http_json(method: str, url: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": raw or exc.reason}
        return exc.code, body


def _via_http(name: str, arguments: dict) -> tuple[int, object]:
    base = board_url().rstrip("/")
    if name == "join":
        return _http_json("POST", base + "/api/join", arguments)
    if name == "feed":
        query = urllib.parse.urlencode({
            "for": arguments.get("agent") or arguments.get("for") or "",
            "since": arguments.get("since") or "",
            "limit": arguments.get("limit") or "",
        })
        return _http_json("GET", base + "/api/feed?" + query)
    if name == "note":
        return _http_json("POST", base + "/api/feed", arguments)
    if name == "task_update":
        task_id = arguments.get("id") or ""
        action = arguments.get("action") or "note"
        return _http_json("POST", f"{base}/api/tasks/{task_id}/{action}", arguments)
    return 404, {"error": f"unknown tool {name}"}


def call_tool(name: str, arguments: dict | None = None) -> dict:
    """Run one shipped board tool. Returns the board JSON."""
    arguments = arguments or {}
    if board_url():
        code, body = _via_http(name, arguments)
    else:
        code, body = _direct(name, arguments)
    if not isinstance(body, dict):
        return {"status": code, "body": body}
    if code >= 400:
        raise BoardToolError(code, str(body.get("error") or "board request failed"))
    return body


def _tool_result(name: str, arguments: dict) -> dict:
    try:
        body = call_tool(name, arguments)
    except BoardToolError as exc:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": str(exc), "status": exc.status})}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    if method and method.startswith("notifications/"):
        return None
    msg_id = message.get("id")
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "dev-room", "version": "1"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": _tool_result(params.get("name") or "", params.get("arguments") or {}),
        }
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}}


def read_message():
    line = sys.stdin.buffer.readline()
    while line and not line.strip():
        line = sys.stdin.buffer.readline()
    if not line:
        return None
    if line.lstrip().startswith(b"{"):
        return json.loads(line.decode())
    headers = {}
    while line and line.strip():
        if b":" in line:
            key, value = line.decode().split(":", 1)
            headers[key.strip().lower()] = value.strip()
        line = sys.stdin.buffer.readline()
    length = int(headers.get("content-length") or 0)
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode())


def write_message(message: dict) -> None:
    # json.dumps escapes newlines inside strings, so one message is one line.
    sys.stdout.buffer.write(json.dumps(message).encode() + b"\n")
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        try:
            message = read_message()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}})
            continue
        if message is None:
            return
        if "method" not in message:
            continue  # a reply to a request this server never sends
        reply = handle(message)
        if reply is not None:
            write_message(reply)


if __name__ == "__main__":
    main()
