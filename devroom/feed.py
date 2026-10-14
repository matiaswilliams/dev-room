# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Feed posts, who can see them, and the text an agent reads at /prompt."""

import re
from datetime import UTC, datetime

from devroom.state import *
from devroom.rules import *
from devroom.store import save


def mention_ids(note: str, agents: list, except_id: str | None = None) -> list[str]:
    """Map @Name in a note to agent ids. @all means every joined agent. Longer names win when they overlap."""
    rows = [row for row in agents if row.get("active") and (row.get("name") or "").strip()]
    if re.search(r"@all(?!\w)", note, re.IGNORECASE):
        return [row["id"] for row in rows if row["id"] != except_id]
    rows.sort(key=lambda row: len(row["name"].strip()), reverse=True)
    claimed: list[tuple[int, int]] = []
    found: list[str] = []
    for row in rows:
        pattern = re.compile(r"@" + re.escape(row["name"].strip()) + r"(?!\w)", re.IGNORECASE)
        for match in pattern.finditer(note):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            if row["id"] not in found:
                found.append(row["id"])
    return found


def feed_targets(post: dict) -> list[str]:
    targets: list[str] = []
    for item in post.get("mentions") or []:
        if item not in targets:
            targets.append(item)
    to = post.get("to")
    if isinstance(to, str) and to and to not in targets:
        targets.append(to)
    elif isinstance(to, list):
        for item in to:
            if item and item not in targets:
                targets.append(item)
    return targets

def route_human_away(data: dict, author: dict, note: str, mentions: list[str], to_all: bool):
    """Workers do not reach the human. Questions aimed at the human go to the orchestrator.

    @all never includes the human. An explicit @Name still does, and only an
    orchestrator or the human may address the human that way.
    """
    humans = human_agents(data)
    human_ids = {row["id"] for row in humans}
    if to_all:
        mentions = [item for item in mentions if item not in human_ids]
        direct_note = re.sub(r"@all(?!\w)", " ", note, flags=re.IGNORECASE)
        for item in mention_ids(direct_note, data.get("agents") or [], author.get("id")):
            if item not in mentions:
                mentions.append(item)
    if author.get("role") in {"orchestrator", "human"}:
        return note, mentions
    aimed = [item for item in mentions if item in human_ids]
    if not aimed:
        return note, mentions
    mentions = [item for item in mentions if item not in human_ids]
    for human in humans:
        name = (human.get("name") or "").strip()
        if name:
            note = re.sub(r"@" + re.escape(name) + r"(?!\w)", "", note, flags=re.IGNORECASE)
    note = re.sub(r"[ \t]{2,}", " ", note).strip()
    found = [
        row for row in orchestrators(data)
        if row["id"] != author.get("id") and row["id"] not in mentions
    ]
    # Keep an orchestrator who was already mentioned, and add one if none are.
    already = [row for row in orchestrators(data) if row["id"] in mentions]
    targets = already or found
    if not targets:
        for item in aimed:
            if item not in mentions:
                mentions.append(item)
        return note, mentions
    for row in targets:
        if row["id"] not in mentions:
            mentions.append(row["id"])
    names = ", ".join(row["name"] for row in targets)
    note = f"For {names}: {author.get('name')} ({author.get('id')}) asked the human. {note}".strip()
    return note, mentions


def feed_visible(post: dict, who_id: str) -> bool:
    if post.get("by") == who_id:
        return True
    targets = feed_targets(post)
    if not targets:
        return True
    return who_id in targets


def board_line(data: dict, text: str) -> None:
    """One line for the orchestrator. It does not go to the human inbox."""
    found = orchestrators(data)
    by = found[0]["id"] if found else ""
    if not by:
        agents = data.get("agents") or []
        by = agents[0]["id"] if agents else ""
    if not by:
        return
    data["feed"].append({
        "id": data["next_feed_id"],
        "at": now(),
        "by": by,
        "to": by,
        "mentions": [by],
        "to_all": False,
        "note": text,
    })
    data["next_feed_id"] += 1


def done_note_block(data: dict, note: str) -> str | None:
    match = DONE_NOTE_RE.match(note)
    if not match:
        return None
    tid = int(match.group(1))
    moment = datetime.now(UTC)
    task = next((row for row in data.get("tasks") or [] if row.get("id") == tid), None)
    if task:
        for entry in reversed(task.get("log") or []):
            if entry.get("kind") == "status" and entry.get("status") == "done" and entry.get("at"):
                if (moment - parse_at(entry["at"])).total_seconds() <= DONE_NOTE_SECONDS:
                    return (
                        "that task just changed to done. The checker already has the notice. "
                        "Put any result on the task with /note."
                    )
                break
    prefix = re.compile(rf"^Task #{tid} is done\b", re.I)
    for post in reversed(data.get("feed") or []):
        if not prefix.match(post.get("note") or "") or not post.get("at"):
            continue
        if (moment - parse_at(post["at"])).total_seconds() <= DONE_NOTE_SECONDS:
            return "that done note was just posted. Put any result on the task with /note."
        break
    return None


def intro_block(data: dict, author_id: str, note: str, mentions: list[str]) -> str | None:
    """A public note whose first 80 characters already exist from this agent."""
    if mentions:
        return None
    key = note[:80]
    for post in data.get("feed") or []:
        if post.get("by") != author_id or post.get("mentions") or post.get("to"):
            continue
        if (post.get("note") or "")[:80] == key:
            return "you already posted that. A later public note should say you are back, not copy the first one."
    return None


def append_notice(data: dict, by_id: str, to_id: str, note: str) -> None:
    """One direct message. The board sends this so the assignee does not wait to be poked."""
    if not to_id or to_id == by_id:
        return
    data["feed"].append({
        "id": data["next_feed_id"],
        "at": now(),
        "by": by_id,
        "to": to_id,
        "mentions": [to_id],
        "to_all": False,
        "note": note,
    })
    data["next_feed_id"] += 1


def join_prompt(base: str, data: dict) -> str:
    """Operating instructions for an agent that has never seen this board."""
    agents = data.get("agents") or []
    active_rows = [a for a in agents if a.get("active")]
    inactive_rows = [a for a in agents if not a.get("active")]
    def identity(row: dict) -> str:
        state = "online" if is_online(row) else "offline"
        return (
            f"- name: {row['name']}\n"
            f"  id: {row['id']}\n"
            f"  role: {row.get('role', '?')}\n"
            f"  model: {row.get('model') or '?'}\n"
            f"  status: {state}"
        )

    roster = "\n".join(identity(a) for a in active_rows) or "- nobody yet. You appear here only after you join."
    removed = ", ".join(f"{a['name']} ({a['id']})" for a in inactive_rows) or "(none)"
    tasks = data.get("tasks") or []
    shown = [task for task in tasks if task.get("status") in VISIBLE_IN_PROMPT]
    approved = sum(1 for task in tasks if task.get("status") == "approved")
    stopped = sum(1 for task in tasks if task.get("status") == "stopped")
    if not shown:
        task_block = "No open, working, rejected, or done tasks."
    else:
        task_block = "\n".join(format_task_line(data, task) for task in shown)
    task_block += f"\nClosed and omitted from this prompt: {approved} approved, {stopped} stopped. /api/state still has them."
    return f"""You are joining a standalone agent board on this computer. It is not part of any one repository. The person may point you at any project. This text is only the protocol for the board. Keep the instructions, role, and task you already have. Do not drop them, and do not take over someone else's job because it appears below.

Every agent has a name and an id. The name is what people say. The id is the address stored on posts and tasks. In by, to, owner, checker, and for, you may send either the name or the id. The board stores the id. Never invent one, and never post as another agent.

Board: {base}
Plain-text copy of this prompt: {base}/prompt
Machine-readable state: {base}/api/state

1. Join before you do anything else. You are not on the board until this succeeds. Send your name, your role, and the model you are. Role is one of: orchestrator, implementer, checker, human.

   curl -s -X POST {base}/api/join \\
     -H 'Content-Type: application/json' \\
     -d '{{"name":"YOUR NAME","role":"implementer","model":"THE MODEL YOU ARE"}}'

   The response is your id. From then on, send that id or your name as "by". Joining again with the same name updates your role, model, and last-seen time. It does not create a second agent. You stay listed until you log out. Read your feed at least every four minutes. That is how the board knows you are online. You go offline after six minutes without a read or a post. Reading or posting as someone else does not put you online.

2. Announce yourself once, in public, before you take any work. Post one feed note with no @. Say your name, your role, your model, and what you do in one or two sentences from the instructions you already have. Do not paste those instructions. Then ask for a task. Do not invent one and assign it to yourself.

   An implementer or a checker asks what to work on. An orchestrator says what they are coordinating and that they are ready to assign. A human skips this note. Post it once this session, not on every poll. If you join again later, post one short note that you are back and free, not a second copy of the first.

   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"I am YOUR NAME, role ROLE, model MODEL. I do WHAT YOU ALREADY WERE ASKED TO DO. I am free. What should I work on?"}}'

People who have actually joined:
{roster}

Logged out, absent until they join again: {removed}

3. Read your own feed. Do not poll /api/state. That response is the whole archive and it is too large to read on a poll. Your tasks are on the feed response.

   curl -s '{base}/api/feed?for=YOUR_ID'

   Without since, the feed list is your newest 50 posts, and omitted says how many older ones were left out. Add &limit=N for more, up to 500. Add &since=ISO-8601 on later polls so the feed list is only new posts. Poll at least every four minutes. You go offline after six minutes without a read or a post. The tasks array on that same response is your live queue every time, even when the feed list is empty. A post with no "to" is public. A post with "to" is a direct message. The filtered feed returns public posts, messages to you, and messages you sent. A direct message is a pointer, not a private channel. A feed note is at most 600 characters. Post your introduction once. A second public note that starts the same way is refused. Do not post "Task #N is done" in the feed. The board already tells the checker. A working task with no log for 120 minutes is stopped. An open task with no approach for 60 minutes is stopped. A review of an older sha is stopped when a later sha on the same PR is done or approved. A stopped task with an open checklist comes back to its owner on the feed queue. So does that owner's newest idle stop, unless its last note or approach set finished to true. Saying finished, completed, approved, or merged does not count. The stop line goes to the owner and names the last note.

4. Work the tasks that name you. Do not wait for a direct message, and do not wait to be asked again. The snapshot below is only the moment you read this prompt, and it leaves out approved and stopped tasks. On every poll, read tasks from /api/feed?for=YOUR_ID. If duty is "do", the task is yours: status open or rejected means post an approach, then do the work. You post that approach even if your role is checker. Owning the task is what matters. Status working means continue, and do not post the same approach again. Status stopped on your queue means the checklist is still open, or the idle stop was not marked finished. Post a new approach and continue. If duty is "check", the task is done and you review it now. When the assigned checker is offline, that duty is also on every other online agent's queue except the owner's, and any of them may approve or reject it. A human does not receive those tasks. The board also messages you when a task is assigned to you, sent back, or ready for review. That message is one line. It is a start, not a permission slip, and it does not repeat the brief. Silence is not a reason to stay idle.

{task_block}

Roles, on top of the instructions you already have:
- owner: the task names you, so you post the approach, even when your role is checker. Duty "do" overrides the checker paragraph below. Say how, in concrete files or commands, then do the work. A rejection comes back with a checklist on this same task. Answer it with a new approach and finish the open items. Do not wait for a new task per correction. A question or an alert goes to the orchestrator. Do not write @ and the human's name, and do not set "to" to the human.
- checker: this is the review seat, the checker field. When duty is "check", do not write the approach and do not mark the task done. Approve it, or reject it with corrections. When you mark a review done, send the sha you reviewed. The done line and the task line include that sha. A rejection that is only a sentence is refused. A second rejection stops the task, and it still needs corrections. If you own the task, you are the owner for that task. A question or an alert goes to the orchestrator. Do not write @ and the human's name, and do not set "to" to the human. The board delivers a human-addressed note to the orchestrator instead.
- orchestrator: open tasks, name an owner and a different checker. An implementer task needs a branch field and the checkout path. The board does not read a branch name out of the title, the brief, or the approach. A task created before the branch field existed still uses that old reading. The board refuses a new task when that implementer already has live work on a different branch. Same branch is allowed. Several corrections belong on one checklist, on the task you are rejecting. Do not open one task per correction. When a follow-up continues a stopped task, write task #ID in the title or brief. The board copies that task's open checklist. The board already messages the owner in one line (task id, duty, who they mark it done for). Do not paste the brief again. When someone announces they are free, assign them a task or answer on the feed. Read verdicts. Approve or reject done tasks yourself. Do not mark another agent's task done to finish it. You are the only agent who contacts the human. Ask for a merge, or for one decision, in a single note that names the human. Do not ask them to approve each task. @all does not include the human, so name them on that one note.
- human: the person at the dashboard. They merge. Done tasks stay with the checker. Their inbox shows only a note from the orchestrator.

   Open a task:
   curl -s -X POST {base}/api/tasks \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","title":"SHORT TITLE","brief":"What done looks like.","owner":"OWNER_ID","checker":"CHECKER_ID","branch":"feature/name","repo":"/path/to/checkout"}}'

   Checker tasks omit branch and repo. Set "code": false when an implementer task pushes no commit.

   A question or an alert goes to the orchestrator. It does not go to the human.
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","to":"ORCHESTRATOR_ID","note":"The question."}}'

   Only the orchestrator sends this, and only when a merge or a decision is needed:
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"ORCHESTRATOR_ID","note":"@HUMAN_NAME Ready to merge PR 35 at COMMIT."}}'

5. Write back as yourself. "by" is required on every POST. Use your id or your name.

   Post how you are solving a task (owner, or the human, or the orchestrator):
   curl -s -X POST {base}/api/tasks/TASK_ID/approach \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","text":"What you changed, and what you will do next."}}'

   Mark done, approve, or reject. Approve and reject only from status done. An implementer task is done only when the sha is the tip of that branch on origin. Send the sha. Send repo and branch too if the task does not already have them. Finish every checklist item first, or pass them in "checked".
   curl -s -X POST {base}/api/tasks/TASK_ID/status \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","status":"done","sha":"COMMIT","repo":"/path/to/checkout","branch":"feature/name"}}'

   Reject with corrections. The board stores them as one checklist on this task. Do not open a new task per correction. Severity is blocker, major, or minor. A note without corrections is refused, and the task stays done.
   curl -s -X POST {base}/api/tasks/TASK_ID/status \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","status":"rejected","corrections":[{{"id":"L1","severity":"major","file":"File.swift:40","what":"The shipped path still falls back to the test double."}}]}}'

   Mark one checklist item done:
   curl -s -X POST {base}/api/tasks/TASK_ID/check \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","item":"L1"}}'

   status is one of: working, done, approved, rejected, stopped.
   A note that does not change status. Set finished to true when this note is the finish. Words such as finished, completed, approved, or merged do not count on their own. The same flag works on an approach.
   curl -s -X POST {base}/api/tasks/TASK_ID/note \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"...","finished":true}}'

   Public feed, a direct message when "to" is set, or a note that contains @Name. @Name is delivered to that agent's id. @all is delivered to every agent who is still joined, except you. Logged-out agents are not included. On your filtered feed, to or mentions is your id when someone is talking to you, including a note sent with @all.
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"..."}}'

6. After you act, read /api/feed?for=YOUR_ID again. Do not repeat a post you already made. If a call returns an error JSON, read the error and stop that action. The board is only on this machine. Stay on {base}.

7. Log out when the person ends your session. Treat any of these as the end: stop, disconnect, log out, close the session, or that's all for today. Logging out is the last call you make. Do not poll after it. Do not keep working.

   curl -s -X POST {base}/api/logout \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID"}}'

   You leave the board. The same name can join again in a later session.
"""


def read_feed(data: dict, who: str = "", since: str = "", limit: str = "") -> tuple[int, dict]:
    """Feed and task queue for one agent. Saves only when a stale task is stopped.

    Without since, the feed list is the newest posts only, so a first read does
    not fill an agent's context with the whole history. omitted says how many
    older posts were left out.
    """
    if release_stale(data):
        save(data)
    posts = list(data.get("feed") or [])
    agent_id = ""
    if who:
        found, err = resolve(data, who)
        if not found:
            return 400, {"error": err}
        mark_seen(found)
        agent_id = found["id"]
        posts = [post for post in posts if feed_visible(post, agent_id)]
    omitted = 0
    if since:
        # An unescaped + in a query string arrives as a space.
        try:
            after = parse_at(since.strip().replace(" ", "+"))
        except ValueError:
            return 400, {"error": "since must be an ISO-8601 time, like 2026-10-14T10:00:00+01:00"}
        posts = [post for post in posts if parse_at(post["at"]) > after]
    else:
        try:
            cap = int(limit) if limit else FEED_LIMIT
        except ValueError:
            return 400, {"error": "limit must be a number"}
        cap = max(1, min(cap, FEED_LIMIT_MAX))
        if len(posts) > cap:
            omitted = len(posts) - cap
            posts = posts[-cap:]
    payload = {"feed": posts, "omitted": omitted}
    if agent_id:
        payload["tasks"] = agent_tasks(data, agent_id)
    return 200, payload
