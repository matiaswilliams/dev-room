# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Load and save the board, and move old history into board-archive/."""

import json
import os
from datetime import UTC, datetime, timedelta

import devroom.state as state
from devroom.state import ARCHIVE_DAYS, DATA


def fresh() -> dict:
    return {
        "title": "dev-room",
        "agents": [],
        "tasks": [],
        "feed": [],
        "next_task_id": 1,
        "next_feed_id": 1,
    }


def load() -> dict:
    if state._BOARD is None:
        if not os.path.exists(DATA):
            state._BOARD = fresh()
            _write(state._BOARD)
        else:
            with open(DATA, encoding="utf-8") as handle:
                state._BOARD = json.load(handle)
    return state._BOARD


def _write(data: dict) -> None:
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA)


def save(data: dict) -> None:
    state._BOARD = data
    state._REV += 1
    _write(data)


def archive_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(DATA)), "board-archive")


def archive_old(data: dict, moment: datetime | None = None, days: float | None = None) -> int:
    """Move closed tasks and feed posts older than days to board-archive/YYYY-MM.jsonl.

    A task is closed when it is approved, or stopped and not handed back to its
    owner. Each archived line is {"kind": "task" | "post", "item": ...}. The
    lines are appended before the board is saved, so a crash in between can
    repeat a line in the archive but never lose one. Returns how many moved.
    """
    # The task rules do not import the store, so this import stays inside the call.
    from devroom.rules import handed_back, last_activity, parse_at
    days = ARCHIVE_DAYS if days is None else days
    if days <= 0:
        return 0
    moment = moment or datetime.now(UTC)
    cutoff = moment - timedelta(days=days)
    keep_ids: set[int] = set()
    for row in data.get("agents") or []:
        keep_ids |= handed_back(data, row["id"])
    lines: dict[str, list[str]] = {}

    def month_of(stamp: datetime) -> str:
        return stamp.astimezone(UTC).strftime("%Y-%m")

    kept_tasks = []
    for task in data.get("tasks") or []:
        touched = last_activity(task)
        old = (
            task.get("status") in {"approved", "stopped"}
            and task.get("id") not in keep_ids
            and touched is not None
            and touched < cutoff
        )
        if not old:
            kept_tasks.append(task)
            continue
        line = json.dumps({"kind": "task", "item": task}, ensure_ascii=False)
        lines.setdefault(month_of(touched), []).append(line)
    kept_posts = []
    for post in data.get("feed") or []:
        try:
            at = parse_at(post.get("at") or "")
        except ValueError:
            kept_posts.append(post)
            continue
        if at >= cutoff:
            kept_posts.append(post)
            continue
        line = json.dumps({"kind": "post", "item": post}, ensure_ascii=False)
        lines.setdefault(month_of(at), []).append(line)
    if not lines:
        return 0
    folder = archive_dir()
    os.makedirs(folder, exist_ok=True)
    moved = 0
    for month, rows in sorted(lines.items()):
        with open(os.path.join(folder, f"{month}.jsonl"), "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
        moved += len(rows)
    data["tasks"] = kept_tasks
    data["feed"] = kept_posts
    return moved
