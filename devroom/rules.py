# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Task rules: branches, reviews, idle stops, and who a task comes back to."""

import os
import re
import subprocess
from datetime import UTC, datetime

from devroom.state import *


def agent(data: dict, aid: str):
    return next((a for a in data["agents"] if a["id"] == aid), None)


def resolve(data: dict, token: str, active_only: bool = False):
    """Find an agent by id or by unique name. Id wins when both could match."""
    raw = (token or "").strip()
    if not raw:
        return None, "missing agent name or id"
    low = raw.lower()
    by_id = next((a for a in data["agents"] if a["id"] == low), None)
    if by_id:
        found = by_id
    else:
        matches = [a for a in data["agents"] if a.get("name", "").strip().lower() == low]
        if not matches:
            return None, f"no agent named {raw!r}. Join first with name, role, and model."
        if len(matches) > 1:
            return None, f"more than one agent is named {raw!r}. Use the id."
        found = matches[0]
    if active_only and not found.get("active"):
        return None, f"{found['name']} ({found['id']}) is removed"
    return found, ""


def address_taken(data: dict, value: str, except_id: str) -> bool:
    low = value.strip().lower()
    for row in data["agents"]:
        if row["id"] == except_id:
            continue
        if row["id"] == low or row.get("name", "").strip().lower() == low:
            return True
    return False


def who(data: dict, aid: str) -> str:
    found = agent(data, aid)
    if not found:
        return aid
    return f"{found['name']} ({found['id']}) · {found.get('role', '?')} · {found.get('model') or '?'}"


def is_online(row: dict) -> bool:
    if not row.get("active", True):
        return False
    seen = row.get("last_seen")
    if not seen:
        return False
    try:
        then = datetime.fromisoformat(seen)
    except ValueError:
        return False
    return (datetime.now(then.tzinfo) - then).total_seconds() <= ONLINE_SECONDS


def slugify(name: str) -> str:
    raw = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:28]
    if not raw or not raw[0].isalpha():
        raw = ("a-" + raw).strip("-")
    return (raw or "agent")[:32]


def id_for_name(data: dict, name: str) -> str:
    same = next(
        (row for row in data["agents"] if row.get("name", "").strip().lower() == name.strip().lower()),
        None,
    )
    if same:
        return same["id"]
    base = slugify(name)
    aid = base
    n = 2
    taken = {row["id"] for row in data["agents"]}
    while aid in taken:
        aid = f"{base[:28]}-{n}"
        n += 1
    return aid


def mark_seen(row: dict) -> None:
    row["last_seen"] = now()


def log(task: dict, by: str, text: str, status: str | None, kind: str, finished: bool = False) -> None:
    entry = {"at": now(), "by": by, "kind": kind, "status": status, "text": text}
    if finished:
        entry["finished"] = True
    task["log"].append(entry)
    task["updated_at"] = now()


def latest_approach(task: dict) -> str:
    for entry in reversed(task["log"]):
        if entry["kind"] == "approach" and entry.get("text"):
            return entry["text"]
    return ""


def rejection_count(task: dict) -> int:
    return sum(1 for entry in task["log"] if entry.get("status") == "rejected")


def clean_branch(value: str) -> str:
    token = (value or "").strip().strip("`\"'.,);")
    if not token or token.lower() in BRANCH_STOP:
        return ""
    if ".." in token or token.startswith("/") or token.endswith("/"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}", token):
        return ""
    return token


def inferred_branch(task: dict) -> str:
    """Branch named in the text. Used only when the task has no branch field."""
    blob = "\n".join([
        task.get("title") or "",
        task.get("brief") or "",
        latest_approach(task),
    ])
    for match in BRANCH_RE.finditer(blob):
        token = clean_branch(match.group(1))
        if token:
            return token
    return ""


def task_branch(task: dict) -> str:
    """The stored branch. A task from before the field existed still reads the text."""
    if "branch" in task:
        return clean_branch(task.get("branch") or "")
    return inferred_branch(task)


def clean_repo(value: str) -> tuple[str, str | None]:
    raw = (value or "").strip()
    if not raw:
        return "", "an implementer task needs a repo path"
    path = os.path.abspath(os.path.expanduser(raw))
    git_dir = os.path.join(path, ".git")
    if not (os.path.isdir(git_dir) or os.path.isfile(git_dir)):
        return "", "repo must be a local git checkout"
    return path, None


def flag(body: dict, key: str, default: bool) -> bool:
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    return bool(value)


def branch_block(data: dict, owner: dict, branch: str) -> str | None:
    """One live branch per implementer. Same branch is allowed. Empty means no block."""
    if owner.get("role") != "implementer":
        return None
    if not branch:
        return "an implementer task needs a branch"
    live = [
        task for task in data["tasks"]
        if task.get("owner") == owner["id"] and task.get("status") in LIVE_OWNER
    ]
    branches: dict[str, int] = {}
    unbranched: list[int] = []
    for task in live:
        found = task_branch(task)
        if found:
            branches.setdefault(found, task["id"])
        else:
            unbranched.append(task["id"])
    if len(branches) > 1:
        listed = ", ".join(f"{name} (#{tid})" for name, tid in sorted(branches.items()))
        return (
            f"{owner['name']} already has more than one live branch: {listed}. "
            "Finish the others before opening more work."
        )
    if len(branches) == 1:
        only = next(iter(branches))
        if branch != only:
            return (
                f"{owner['name']} is on branch {only} (task #{branches[only]}). "
                f"Finish that branch before opening {branch}."
            )
        return None
    if unbranched:
        ids = ", ".join(f"#{tid}" for tid in unbranched)
        return (
            f"{owner['name']} has live tasks with no branch recorded ({ids}). "
            "Finish them, or set their branch, before opening another task."
        )
    return None


def normalize_corrections(raw, required: bool = False):
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None, "corrections must be a list of {id, severity, file, what}"
    if required and not raw:
        return None, "a rejection needs corrections, each with id, severity, file, and what"
    items = []
    seen: set[str] = set()
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            return None, f"correction {index} must be an object with id, severity, file, and what"
        cid = str(row.get("id") or "").strip()
        severity = str(row.get("severity") or "").strip().lower()
        file = str(row.get("file") or "").strip()
        what = str(row.get("what") or "").strip()
        if not ITEM_ID_RE.match(cid):
            return None, f"correction {index} needs an id like L1"
        if cid in seen:
            return None, f"duplicate correction id {cid}"
        seen.add(cid)
        if severity not in SEVERITIES:
            return None, f"correction {cid} severity must be blocker, major, or minor"
        if not file or len(file) > 240 or any(ch in file for ch in "\n\r"):
            return None, f"correction {cid} needs a file"
        if not what or len(what) > 500:
            return None, f"correction {cid} needs what is wrong"
        items.append({
            "id": cid,
            "severity": severity,
            "file": file,
            "what": what,
            "done": False,
        })
    return items, None


def format_corrections(items: list[dict]) -> str:
    return "\n".join(
        f"{item['id']} [{item['severity']}] {item['file']}: {item['what']}"
        for item in items
    )


def task_needs_sha(data: dict, task: dict) -> bool:
    if "code" in task:
        return bool(task["code"])
    owner = agent(data, task.get("owner"))
    return bool(owner and owner.get("role") == "implementer")


def git_out(repo: str, args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def verify_pushed(repo: str, branch: str, sha: str) -> tuple[str | None, str | None]:
    """Return (full sha, error). The sha must be the tip of origin/branch."""
    short = (sha or "").strip().lower()
    if not SHA_RE.match(short):
        return None, "sha must be 7 to 40 hex characters"
    branch = clean_branch(branch)
    if not branch:
        return None, "branch is required to check the pushed commit"
    path, err = clean_repo(repo)
    if err:
        return None, err
    code, out, _err = git_out(path, ["rev-parse", "--verify", short + "^{commit}"])
    if code != 0:
        return None, "that sha is not in the checkout"
    full = out.strip().lower()
    code, out, errtext = git_out(path, ["ls-remote", "origin", f"refs/heads/{branch}"])
    if code != 0:
        detail = (errtext or out or "ls-remote failed").strip().splitlines()
        return None, "could not read origin: " + (detail[-1] if detail else "ls-remote failed")
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return None, f"origin has no branch {branch}"
    remote = lines[0].split()[0].lower()
    if remote == full or remote.startswith(short):
        return full, None
    return None, (
        f"origin/{branch} is {remote[:12]}, not {full[:12]}. "
        "Push this commit so it is the branch tip, then mark done with that sha."
    )


def checklist_text(task: dict) -> str:
    items = task.get("checklist") or []
    if not items:
        return ""
    parts = []
    for item in items:
        mark = "done" if item.get("done") else "open"
        parts.append(f"{item['id']} [{item['severity']}] {mark}: {item['file']} — {item['what']}")
    return "\n  checklist: " + "; ".join(parts)


def format_task_line(data: dict, task: dict) -> str:
    how = latest_approach(task) or "no approach posted yet"
    branch = task_branch(task) or "(none)"
    repo = task.get("repo") or "(none)"
    head = (task.get("head") or "").strip()
    head_line = f"  head: {head}\n" if head else ""
    return (
        f"- #{task['id']} [{task['status']}] {task['title']}\n"
        f"  owner {who(data, task['owner'])} · checker {who(data, task['checker'])}\n"
        f"  branch: {branch} · repo: {repo}\n"
        f"{head_line}"
        f"  brief: {task.get('brief') or '(none)'}\n"
        f"  how: {how}{checklist_text(task)}"
    )


def human_agents(data: dict) -> list[dict]:
    return [row for row in data.get("agents") or [] if row.get("role") == "human"]


def orchestrators(data: dict) -> list[dict]:
    rows = [row for row in data.get("agents") or [] if row.get("role") == "orchestrator"]
    active = [row for row in rows if row.get("active")]
    return active or rows

def checker_offline(data: dict, task: dict) -> bool:
    row = agent(data, task.get("checker"))
    if not row or not row.get("active", True):
        return True
    return not is_online(row)


def can_cover_review(data: dict, task: dict, who_id: str) -> bool:
    """The assigned checker is offline, so another online agent may review. The owner may not."""
    if task.get("status") != "done" or not checker_offline(data, task):
        return False
    if who_id in {task.get("owner"), task.get("checker")}:
        return False
    row = agent(data, who_id)
    if not row or not row.get("active", True) or row.get("role") == "human":
        return False
    return is_online(row)


def open_checklist(task: dict) -> bool:
    return any(not item.get("done") for item in task.get("checklist") or [])


def last_work_entry(task: dict) -> dict | None:
    for entry in reversed(task.get("log") or []):
        if entry.get("kind") in {"note", "approach"} and (entry.get("text") or "").strip():
            return entry
    return None


def stall_stopped(task: dict) -> bool:
    logs = task.get("log") or []
    if not logs:
        return False
    if task.get("stop_kind"):
        return task["stop_kind"] == "idle"
    # Boards from before stop_kind only have the log text.
    return "No log for 120 minutes" in (logs[-1].get("text") or "")


def handed_back(data: dict, who_id: str) -> set[int]:
    """Stopped correction rounds, plus the newest idle stop that was not marked finished."""
    ids: set[int] = set()
    newest = None
    newest_at = None
    for task in data.get("tasks") or []:
        if task.get("owner") != who_id or task.get("status") != "stopped":
            continue
        if open_checklist(task):
            ids.add(task["id"])
        if not stall_stopped(task):
            continue
        entry = last_work_entry(task)
        if entry and entry.get("finished") is True:
            continue
        touched = last_activity(task)
        if newest is None or (touched and (newest_at is None or touched > newest_at)):
            newest = task["id"]
            newest_at = touched
    if newest is not None:
        ids.add(newest)
    return ids


def agent_tasks(data: dict, who_id: str) -> list[dict]:
    """Work this agent should act on now. Owners start open, working, and rejected tasks. Checkers review done tasks."""
    returned = handed_back(data, who_id)
    rows = []
    for task in data.get("tasks") or []:
        status = task.get("status")
        if task.get("owner") == who_id and status in {"open", "working", "rejected"}:
            duty = "do"
        elif task.get("owner") == who_id and task.get("id") in returned:
            duty = "do"
        elif status == "done" and (
            task.get("checker") == who_id or can_cover_review(data, task, who_id)
        ):
            duty = "check"
        else:
            continue
        rows.append({
            "id": task["id"],
            "title": task.get("title") or "",
            "brief": task.get("brief") or "",
            "status": status,
            "owner": task.get("owner"),
            "checker": task.get("checker"),
            "approach": latest_approach(task),
            "updated_at": task.get("updated_at"),
            "duty": duty,
            "branch": task.get("branch") or task_branch(task),
            "repo": task.get("repo") or "",
            "code": task_needs_sha(data, task),
            "head": task.get("head") or "",
            "checklist": task.get("checklist") or [],
        })
    return rows


def parse_at(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def last_activity(task: dict) -> datetime | None:
    stamps = [entry.get("at") for entry in (task.get("log") or []) if entry.get("at")]
    if task.get("updated_at"):
        stamps.append(task["updated_at"])
    if task.get("created_at"):
        stamps.append(task["created_at"])
    if not stamps:
        return None
    return max(parse_at(stamp) for stamp in stamps)


def task_blob(task: dict) -> str:
    return "\n".join([
        task.get("title") or "",
        task.get("brief") or "",
        latest_approach(task),
        task.get("head") or "",
    ])


def cited_prs(task: dict) -> set[str]:
    return set(PR_RE.findall(task_blob(task)))


def cited_shas(task: dict) -> set[str]:
    found = set()
    head = (task.get("head") or "").strip().lower()
    if head:
        found.add(head)
    for match in SHA_FIND_RE.findall(task_blob(task)):
        if re.search(r"[a-f]", match, re.I):
            found.add(match.lower())
    return found


def same_commit(left: str, right: str) -> bool:
    a, b = left.lower(), right.lower()
    return a == b or a.startswith(b) or b.startswith(a)


def is_review_task(data: dict, task: dict) -> bool:
    if task.get("code") is False:
        return True
    owner = agent(data, task.get("owner"))
    return bool(owner and owner.get("role") == "checker")

def stop_notice(task: dict, reason: str) -> str:
    checklist = task.get("checklist") or []
    open_n = sum(1 for item in checklist if not item.get("done"))
    count = f" Checklist open {open_n}/{len(checklist)}." if checklist else ""
    previous = ""
    for entry in reversed(task.get("log") or []):
        if entry.get("kind") == "status":
            continue
        previous = re.sub(r"@\w+", "", (entry.get("text") or "").strip()).replace("\n", " ")
        previous = re.sub(r"\s{2,}", " ", previous).strip()
        break
    extra = f" Last note: {previous[:180]}." if previous else ""
    return f"Task #{task['id']} stopped. {reason}{count}{extra}"


def stop_working(data: dict, task: dict, reason: str, kind: str = "") -> None:
    # feed imports these rules, so this import stays inside the call.
    from devroom.feed import append_notice, board_line
    task["status"] = "stopped"
    if kind:
        task["stop_kind"] = kind
    found = orchestrators(data)
    by = found[0]["id"] if found else (task.get("owner") or "board")
    log(task, by, reason, "stopped", "status")
    line = stop_notice(task, reason)
    board_line(data, line)
    append_notice(data, by, task.get("owner") or "", line)


def release_stale(data: dict) -> bool:
    """Stop a review pinned to an older sha, then a working task idle for 120 minutes."""
    changed = False
    moment = datetime.now(UTC)
    for task in list(data.get("tasks") or []):
        if task.get("status") != "working" or not is_review_task(data, task):
            continue
        prs = cited_prs(task)
        shas = cited_shas(task)
        touched = last_activity(task)
        if not prs or not shas or not touched:
            continue
        for other in data["tasks"]:
            if other.get("id") == task.get("id") or other.get("status") not in {"done", "approved"}:
                continue
            shared = cited_prs(other) & prs
            if not shared:
                continue
            other_at = last_activity(other)
            later = cited_shas(other)
            if not other_at or other_at <= touched or not later:
                continue
            if any(same_commit(old, new) for old in shas for new in later):
                continue
            pr = sorted(shared)[0]
            stop_working(data, task, f"Its sha is older than a later done sha on PR #{pr}.", "superseded")
            changed = True
            break
    for task in list(data.get("tasks") or []):
        if task.get("status") != "working":
            continue
        touched = last_activity(task)
        if touched and (moment - touched).total_seconds() >= STALE_SECONDS:
            stop_working(data, task, "No log for 120 minutes.", "idle")
            changed = True
    for task in list(data.get("tasks") or []):
        if task.get("status") != "open" or latest_approach(task):
            continue
        touched = last_activity(task)
        if touched and (moment - touched).total_seconds() >= OPEN_IDLE_SECONDS:
            stop_working(data, task, "No approach for 60 minutes.", "no-approach")
            changed = True
    return changed


def copied_open_checks(data: dict, title: str, brief: str) -> list[dict]:
    """Open checklist rows from a stopped task named in the new brief."""
    ids: list[int] = []
    for match in TASK_REF_RE.finditer(f"{title}\n{brief}"):
        tid = int(match.group(1))
        if tid not in ids:
            ids.append(tid)
    items = []
    seen: set[str] = set()
    for tid in ids:
        task = next((row for row in data.get("tasks") or [] if row.get("id") == tid), None)
        if not task or task.get("status") != "stopped":
            continue
        for item in task.get("checklist") or []:
            if item.get("done") or item.get("id") in seen:
                continue
            seen.add(item["id"])
            items.append({
                "id": item["id"],
                "severity": item["severity"],
                "file": item["file"],
                "what": item["what"],
                "done": False,
            })
    return items


def origin_url(path: str) -> str:
    code, out, _err = git_out(path, ["remote", "get-url", "origin"])
    if code != 0:
        return ""
    return out.strip().rstrip("/")


def same_origin(left: str, right: str) -> bool:
    first, second = origin_url(left), origin_url(right)
    return bool(first and first == second)

