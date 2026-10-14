# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""HTTP routes. One process serves the dashboard and the JSON API."""

import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from devroom import state
from devroom.state import *
from devroom.store import *
from devroom.rules import *
from devroom.feed import *


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def apply_handler(method: str, *args) -> tuple[int, object]:
    """Run a Handler operation and return its status and body. No socket."""
    sent: dict = {}

    class Bridge:
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            sent["code"] = code
            sent["body"] = body
            sent["ctype"] = ctype

    bridge = Bridge()
    bridge._require_author = lambda data, body: Handler._require_author(bridge, data, body)
    bridge._done_ready = lambda data, task, body: Handler._done_ready(bridge, data, task, body)
    getattr(Handler, method)(bridge, *args)
    if "code" not in sent:
        return 500, {"error": "no response"}
    return sent["code"], sent["body"]


def board_call(op: str, payload: dict | None = None) -> tuple[int, object]:
    """Join, read the feed, post a note, or update a task. HTTP and MCP both use this."""
    payload = payload or {}
    with LOCK:
        data = load()
        if op == "feed":
            return read_feed(
                data,
                str(payload.get("for") or payload.get("agent") or ""),
                str(payload.get("since") or ""),
                str(payload.get("limit") or ""),
            )
        if op == "join":
            return apply_handler("_join", data, payload)
        if op == "note":
            return apply_handler("_post_feed", data, payload)
        if op == "task":
            return apply_handler(
                "_task_action",
                data,
                str(payload.get("id") or ""),
                str(payload.get("action") or "note"),
                payload,
            )
        return 404, {"error": "unknown operation"}


def host_name(value: str) -> str:
    """The host part of a Host header or an Origin, without scheme or port."""
    raw = (value or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def local_request(headers) -> str | None:
    """None when the request comes from this machine. Otherwise the reason it is refused."""
    if host_name(headers.get("Host") or "") not in LOCAL_HOSTS:
        return "the board only answers to localhost or 127.0.0.1"
    origin = headers.get("Origin")
    if origin and (origin == "null" or host_name(origin) not in LOCAL_HOSTS):
        return "requests from other sites are refused"
    return None


def sweep_forever() -> None:
    """Stop stale tasks and archive old history, even when nobody polls."""
    while True:
        threading.Event().wait(SWEEP_SECONDS)
        try:
            with LOCK:
                data = load()
                stopped = release_stale(data)
                if archive_old(data) or stopped:
                    save(data)
        except Exception as exc:  # keep sweeping after a bad row
            print(f"sweep failed: {exc}", file=sys.stderr, flush=True)


class BoardServer(ThreadingHTTPServer):
    # A stuck client must not keep the process alive or fill the accept queue.
    daemon_threads = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(10)

    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", etag: str = "") -> None:
        # Queued so the socket write happens after the board lock is released.
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self._pending = (code, raw, ctype, etag)

    def _flush(self) -> None:
        pending = getattr(self, "_pending", None)
        if not pending:
            return
        self._pending = None
        code, raw, ctype, etag = pending
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"__invalid__": True}
        return body if isinstance(body, dict) else {"__invalid__": True}

    def _require_author(self, data: dict, body: dict):
        found, err = resolve(data, body.get("by") or "", active_only=True)
        if not found:
            self._send(400, {"error": err or "by must be an agent who has joined"})
            return None
        mark_seen(found)
        return found

    def do_GET(self):
        try:
            self._do_GET()
        finally:
            self._flush()

    def _do_GET(self):
        refused = local_request(self.headers)
        if refused:
            return self._send(403, {"error": refused})
        path, _, query = self.path.partition("?")
        if path == "/favicon.ico":
            try:
                with open(FAVICON, "rb") as handle:
                    raw = handle.read()
            except OSError:
                return self._send(404, {"error": "no icon"})
            return self._send(200, raw, "image/x-icon")
        if path in ("/", "/index.html", "/prompt"):
            host = self.headers.get("Host") or "localhost:8791"
            base = "http://" + host
            with LOCK:
                text = join_prompt(base, load())
            if path == "/prompt":
                return self._send(200, text.encode(), "text/plain; charset=utf-8")
            with open(INDEX, encoding="utf-8") as f:
                html = f.read()
            block = "<pre id=\"join-prompt\">" + _escape(text) + "</pre>"
            html = html.replace("<!-- PROMPT -->", block, 1)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/state":
            with LOCK:
                data = load()
                # Online flags change with time, not only with saves, so the tag
                # also turns over every 30 seconds.
                etag = f'"{state._REV}-{int(datetime.now(UTC).timestamp()) // 30}"'
                if self.headers.get("If-None-Match") == etag:
                    return self._send(304, b"", etag=etag)
                agents = []
                for row in data["agents"]:
                    item = dict(row)
                    item["online"] = is_online(row)
                    agents.append(item)
                tasks = []
                for task in data["tasks"]:
                    item = dict(task)
                    item["approach"] = latest_approach(task)
                    tasks.append(item)
                body = json.dumps({**data, "agents": agents, "tasks": tasks}, ensure_ascii=False).encode()
            return self._send(200, body, etag=etag)
        if path == "/api/feed":
            params = parse_qs(query)
            who = (params.get("for") or [""])[0]
            since = (params.get("since") or [""])[0]
            limit = (params.get("limit") or [""])[0]
            with LOCK:
                code, payload = read_feed(load(), who, since, limit)
            return self._send(code, payload)
        return self._send(404, {"error": "unknown path"})

    def do_POST(self):
        try:
            self._do_POST()
        finally:
            self._flush()

    def _do_POST(self):
        refused = local_request(self.headers)
        if refused:
            return self._send(403, {"error": refused})
        # A web page can send text/plain without a preflight. JSON needs one.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, {"error": "send Content-Type: application/json"})
        path = self.path.split("?")[0]
        body = self._body()
        if body.get("__invalid__"):
            return self._send(400, {"error": "invalid JSON"})

        with LOCK:
            data = load()
            if path == "/api/join":
                return self._join(data, body)
            if path == "/api/logout":
                return self._logout(data, body)
            if path == "/api/heartbeat":
                author = self._require_author(data, body)
                if not author:
                    return None
                # last_seen lives in memory. The next real save writes it.
                return self._send(200, {**author, "online": True})
            if path == "/api/agents":
                return self._add_agent(data, body)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "agents"]:
                return self._agent_action(data, parts[2], parts[3], body)
            if path == "/api/feed":
                return self._post_feed(data, body)
            if path == "/api/tasks":
                return self._add_task(data, body)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                return self._task_action(data, parts[2], parts[3], body)
            return self._send(404, {"error": "unknown path"})

    def _agent_action(self, data: dict, aid: str, action: str, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        found = agent(data, aid)
        if not found:
            return self._send(404, {"error": "no such agent"})
        if author["role"] not in {"human", "orchestrator"} and author["id"] != found["id"]:
            return self._send(403, {"error": "only the human or an orchestrator can remove or restore another agent"})
        if action == "remove":
            found["active"] = False
        elif action == "restore":
            found["active"] = True
        else:
            return self._send(404, {"error": "use remove or restore"})
        save(data)
        return self._send(200, found)

    def _join(self, data: dict, body: dict):
        name = (body.get("name") or "").strip()
        role = (body.get("role") or "").strip().lower()
        model = (body.get("model") or "").strip()
        if not name or len(name) > 60:
            return self._send(400, {"error": "name is required"})
        if role not in ROLES:
            return self._send(400, {"error": f"role must be one of {sorted(ROLES)}"})
        if not model or len(model) > 80:
            return self._send(400, {"error": "model is required"})
        aid = id_for_name(data, name)
        found = agent(data, aid)
        if found:
            found["name"] = name
            found["role"] = role
            found["model"] = model
            found["active"] = True
            mark_seen(found)
            row = found
            code = 200
        else:
            row = {
                "id": aid,
                "name": name,
                "role": role,
                "model": model,
                "active": True,
                "joined_at": now(),
                "last_seen": now(),
            }
            data["agents"].append(row)
            code = 201
        save(data)
        row["online"] = True
        return self._send(code, row)

    def _logout(self, data: dict, body: dict):
        found, err = resolve(data, body.get("by") or "", active_only=False)
        if not found:
            return self._send(400, {"error": err or "by must be an agent who has joined"})
        found["active"] = False
        save(data)
        found["online"] = False
        return self._send(200, found)

    def _add_agent(self, data: dict, body: dict):
        return self._send(400, {"error": "join with POST /api/join and a name, role, and model"})

    def _post_feed(self, data: dict, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        note = (body.get("note") or "").strip()
        if not note:
            return self._send(400, {"error": "note is required"})
        if len(note) > NOTE_MAX:
            return self._send(400, {
                "error": "a feed note is at most 600 characters. Put the detail on the task with /note, or on the reject checklist.",
            })
        blocked = done_note_block(data, note)
        if blocked:
            return self._send(400, {"error": blocked})
        raw_to = body.get("to")
        to_token = raw_to.strip() if isinstance(raw_to, str) else ""
        mentions = mention_ids(note, data["agents"], author["id"])
        to_all = bool(re.search(r"@all(?!\w)", note, re.IGNORECASE))
        if to_token:
            target, err = resolve(data, to_token)
            if not target:
                return self._send(400, {"error": err})
            if target["id"] not in mentions:
                mentions.append(target["id"])
        note, mentions = route_human_away(data, author, note, mentions, to_all)
        repeated = intro_block(data, author["id"], note, mentions)
        if repeated:
            return self._send(400, {"error": repeated})
        entry = {
            "id": data["next_feed_id"],
            "at": now(),
            "by": author["id"],
            "to": mentions[0] if len(mentions) == 1 else (mentions or None),
            "mentions": mentions,
            "to_all": to_all,
            "note": note,
        }
        data["next_feed_id"] += 1
        data["feed"].append(entry)
        save(data)
        return self._send(201, entry)

    def _add_task(self, data: dict, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        title = (body.get("title") or "").strip()
        if not title:
            return self._send(400, {"error": "title is required"})
        owner, owner_err = resolve(data, body.get("owner") or "", active_only=True)
        checker, checker_err = resolve(data, body.get("checker") or "", active_only=True)
        if not owner or not checker:
            return self._send(400, {"error": owner_err or checker_err or "owner and checker must be active agents"})
        if owner["id"] == checker["id"]:
            return self._send(400, {"error": "checker must be a different agent"})
        brief = (body.get("brief") or "").strip()
        code = flag(body, "code", owner.get("role") == "implementer")
        branch = clean_branch(str(body.get("branch") or ""))
        if body.get("branch") and not branch:
            return self._send(400, {"error": "branch must be a branch name, without spaces"})
        repo = ""
        if owner.get("role") == "implementer":
            blocked = branch_block(data, owner, branch)
            if blocked:
                code_no = 400 if blocked.endswith("needs a branch") else 409
                return self._send(code_no, {"error": blocked})
            if code:
                repo, repo_err = clean_repo(str(body.get("repo") or ""))
                if repo_err:
                    return self._send(400, {"error": repo_err})
        elif body.get("repo"):
            repo, repo_err = clean_repo(str(body.get("repo") or ""))
            if repo_err:
                return self._send(400, {"error": repo_err})
        checklist, list_err = normalize_corrections(body.get("checklist") or [])
        if list_err:
            return self._send(400, {"error": list_err})
        carried = copied_open_checks(data, title, brief)
        if carried:
            own = {item["id"] for item in checklist}
            checklist = [item for item in carried if item["id"] not in own] + checklist
        task = {
            "id": data["next_task_id"],
            "title": title,
            "brief": brief,
            "owner": owner["id"],
            "checker": checker["id"],
            "status": "open",
            "created_at": now(),
            "updated_at": now(),
            "log": [],
            "branch": branch,
            "repo": repo,
            "code": code if owner.get("role") == "implementer" else flag(body, "code", False),
            "checklist": checklist,
        }
        data["next_task_id"] += 1
        log(task, author["id"], brief or "Opened", "open", "status")
        data["tasks"].append(task)
        append_notice(
            data,
            author["id"],
            owner["id"],
            f"Task #{task['id']} is yours. Duty: do. Mark it done for {checker['name']} ({checker['id']}).",
        )
        save(data)
        task["approach"] = ""
        return self._send(201, task)

    def _done_ready(self, data: dict, task: dict, body: dict):
        """Validate a done move without changing the task. Returns ready, error, sha, repo, branch, checked ids."""
        checklist = task.get("checklist") or []
        raw_checked = body.get("checked") or []
        if isinstance(raw_checked, str):
            raw_checked = [part.strip() for part in raw_checked.split(",") if part.strip()]
        if not isinstance(raw_checked, list):
            return False, "checked must be a list of checklist ids", None, None, None, set()
        checked = {str(item).strip() for item in raw_checked}
        known = {item.get("id") for item in checklist}
        unknown = sorted(cid for cid in checked if cid not in known)
        if unknown:
            return False, "no such checklist item: " + ", ".join(unknown), None, None, None, set()
        open_items = [
            item["id"] for item in checklist
            if not item.get("done") and item.get("id") not in checked
        ]
        if open_items:
            return False, "checklist still open: " + ", ".join(open_items), None, None, None, set()

        full = None
        repo_path = None
        branch_name = None
        if task_needs_sha(data, task):
            if "branch" in task:
                known = clean_branch(task.get("branch") or "")
            else:
                known = inferred_branch(task)
            sent = clean_branch(str(body.get("branch") or ""))
            if body.get("branch") and not sent:
                return False, "branch must be a branch name, without spaces", None, None, None, set()
            if known and sent and known != sent:
                return False, f"this task is on branch {known}", None, None, None, set()
            branch_name = sent or known
            stored_repo = (task.get("repo") or "").strip()
            sent_repo = str(body.get("repo") or "").strip()
            if stored_repo and sent_repo:
                stored_path, stored_err = clean_repo(stored_repo)
                sent_path, sent_err = clean_repo(sent_repo)
                if sent_err:
                    return False, sent_err, None, None, None, set()
                if stored_err:
                    return False, stored_err, None, None, None, set()
                if stored_path != sent_path and not same_origin(stored_path, sent_path):
                    return False, f"this task is in repo {stored_path}", None, None, None, set()
                repo_path = sent_path
            else:
                repo_path, repo_err = clean_repo(sent_repo or stored_repo)
                if repo_err:
                    return False, repo_err, None, None, None, set()
            # The network check runs after this returns, outside the board lock.
            short = str(body.get("sha") or "").strip().lower()
            if not SHA_RE.match(short):
                return False, "sha must be 7 to 40 hex characters", None, None, None, set()
            full = short
        else:
            if body.get("repo"):
                repo_path, repo_err = clean_repo(str(body.get("repo") or ""))
                if repo_err:
                    return False, repo_err, None, None, None, set()
            if body.get("branch"):
                branch_name = clean_branch(str(body.get("branch") or ""))
                if not branch_name:
                    return False, "branch must be a branch name, without spaces", None, None, None, set()
            if is_review_task(data, task):
                short = str(body.get("sha") or "").strip().lower()
                if not SHA_RE.match(short):
                    return (
                        False,
                        "a review must include the sha you reviewed, 7 to 40 hex characters",
                        None, None, None, set(),
                    )
                full = short
        return True, None, full, repo_path, branch_name, checked

    def _task_action(self, data: dict, raw_id: str, action: str, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        try:
            tid = int(raw_id)
        except ValueError:
            return self._send(404, {"error": "bad task id"})
        task = next((t for t in data["tasks"] if t["id"] == tid), None)
        if not task:
            return self._send(404, {"error": "no such task"})
        text = (body.get("text") or body.get("note") or "").strip()
        human = author.get("role") == "human"
        orchestrator = author["role"] == "orchestrator"
        is_owner = author["id"] == task["owner"]
        is_checker = author["id"] == task["checker"]

        if action == "note":
            if not text:
                return self._send(400, {"error": "note is required"})
            log(task, author["id"], text, None, "note", finished=flag(body, "finished", False))
        elif action == "approach":
            if not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner posts how this is being solved"})
            if not text:
                return self._send(400, {"error": "text is required"})
            if task["status"] in {"approved", "stopped"}:
                return self._send(409, {"error": "this task is closed"})
            if task["status"] in {"open", "rejected"}:
                task["status"] = "working"
            log(task, author["id"], text, task["status"], "approach", finished=flag(body, "finished", False))
        elif action == "check":
            if not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner can mark a checklist item"})
            item_id = str(body.get("item") or "").strip()
            found = next((item for item in task.get("checklist") or [] if item.get("id") == item_id), None)
            if not found:
                return self._send(400, {"error": "no such checklist item"})
            found["done"] = not flag(body, "open", False)
            log(task, author["id"], f"{item_id} {'open' if not found['done'] else 'done'}", None, "check")
        elif action == "status":
            status = body.get("status")
            if status not in STATUSES or status == "open":
                return self._send(400, {"error": "status must be working, done, approved, rejected, or stopped"})
            if task["status"] in {"approved", "stopped"}:
                return self._send(409, {"error": "this task is closed"})
            if status in {"working", "done"} and not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner can move this forward"})
            corrections = None
            full_sha = None
            repo_path = None
            branch_name = None
            checked_ids: set[str] = set()
            if status in {"approved", "rejected"}:
                if task["status"] != "done":
                    return self._send(409, {"error": "only a done task can be approved or rejected"})
                if not (is_checker or human or can_cover_review(data, task, author["id"])):
                    return self._send(403, {"error": "only the checker can approve or reject"})
                if status == "rejected":
                    corrections, corr_err = normalize_corrections(body.get("corrections"), required=True)
                    if corr_err:
                        return self._send(400, {"error": corr_err})
                    text = format_corrections(corrections)
                    extra = (body.get("note") or body.get("text") or "").strip()
                    if extra:
                        text = text + "\n" + extra
                    if rejection_count(task) >= 1:
                        status = "stopped"
                        text = text + "\nsecond rejection, task stopped."
            if status == "stopped" and not (human or orchestrator or is_checker):
                return self._send(403, {"error": "only the orchestrator, checker, or you can stop a task"})
            if status == "done":
                ready, done_err, full_sha, repo_path, branch_name, checked_ids = self._done_ready(
                    data, task, body
                )
                if not ready:
                    return self._send(400, {"error": done_err})
                if task_needs_sha(data, task):
                    # git ls-remote can take many seconds. Drop the lock so polls still answer.
                    LOCK.release()
                    try:
                        full_sha, sha_err = verify_pushed(repo_path or "", branch_name or "", full_sha or "")
                    finally:
                        LOCK.acquire()
                    if sha_err:
                        return self._send(400, {"error": sha_err})
                    data = load()
                    task = next((item for item in data["tasks"] if item["id"] == tid), None)
                    if not task:
                        return self._send(404, {"error": "no such task"})
                    fresh = agent(data, author["id"])
                    if fresh:
                        mark_seen(fresh)
                        author = fresh
                    if task["status"] in {"approved", "stopped"}:
                        return self._send(409, {"error": "this task changed while the push was checked"})
                    ready, done_err, _, repo_path, branch_name, checked_ids = self._done_ready(
                        data, task, body
                    )
                    if not ready:
                        return self._send(400, {"error": done_err})
            task["status"] = status
            if corrections is not None:
                task["checklist"] = corrections
            if status == "done":
                for item in task.get("checklist") or []:
                    if item.get("id") in checked_ids:
                        item["done"] = True
                if repo_path:
                    task["repo"] = repo_path
                if branch_name:
                    task["branch"] = branch_name
                if full_sha:
                    task["head"] = full_sha
            log(task, author["id"], text, status, "status")
            release_stale(data)
            if status == "done":
                head = (task.get("head") or "").strip()
                sha_bit = f" Sha: {head}." if head else ""
                append_notice(
                    data,
                    author["id"],
                    task["checker"],
                    f"Task #{tid} is done.{sha_bit} Duty: check. Approve it, or reject it with corrections.",
                )
            elif status == "rejected":
                append_notice(
                    data,
                    author["id"],
                    task["owner"],
                    f"Task #{tid} was rejected. Duty: do. The checklist is on the task. Post a new approach and finish the open items.",
                )
            elif status == "stopped":
                append_notice(
                    data,
                    author["id"],
                    task["owner"],
                    f"Task #{tid} is stopped. Read the checklist on the task.",
                )
        else:
            return self._send(404, {"error": "use approach, note, check, or status"})
        save(data)
        task["approach"] = latest_approach(task)
        return self._send(200, task)
