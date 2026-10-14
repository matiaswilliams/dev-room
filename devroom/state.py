# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Paths, limits, and the in-memory board for this process."""

import os
import re
import threading
from datetime import UTC, datetime

# server.py stays at the repository root. This file lives in devroom/.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DEV_ROOM_* is the current name. AGENT_BOARD_* still works.
DATA = (
    os.environ.get("DEV_ROOM_DATA")
    or os.environ.get("AGENT_BOARD_DATA")
    or os.path.join(ROOT, "board.json")
)
INDEX = os.path.join(ROOT, "index.html")
FAVICON = os.path.join(ROOT, "favicon.ico")
LOCK = threading.Lock()

STATUSES = {"open", "working", "done", "approved", "rejected", "stopped"}
ROLES = {"orchestrator", "implementer", "checker", "human"}
SEVERITIES = {"blocker", "major", "minor"}
LIVE_OWNER = {"open", "working", "rejected"}
VISIBLE_IN_PROMPT = {"open", "working", "rejected", "done"}
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
BRANCH_RE = re.compile(r"\bbranch\s+([A-Za-z0-9][A-Za-z0-9._/-]{0,80})", re.I)
BRANCH_STOP = {
    "from", "the", "a", "an", "this", "that", "and", "then", "current",
    "its", "their", "your", "own", "another", "same",
}
# A poll every four minutes still counts. The prompt says four minutes.
ONLINE_SECONDS = 360
# The board only answers to this machine. A Host or Origin outside this list is
# a web page or a DNS rebinding attempt, not a local agent.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SWEEP_SECONDS = 60
# A feed read without since returns only this many of the newest posts.
FEED_LIMIT = 50
FEED_LIMIT_MAX = 500
# Closed tasks and feed posts older than this move to board-archive/.
# Zero turns archiving off.
try:
    ARCHIVE_DAYS = float(os.environ.get("DEV_ROOM_ARCHIVE_DAYS") or 7)
except ValueError:
    ARCHIVE_DAYS = 7.0

# One process owns the board. Polls read this copy. Disk is for restarts.
_BOARD: dict | None = None
# Goes up on every save. The dashboard uses it to skip unchanged state.
_REV = 0

OPEN_IDLE_SECONDS = 60 * 60
STALE_SECONDS = 120 * 60
DONE_NOTE_SECONDS = 120
NOTE_MAX = 600
PR_RE = re.compile(r"\bPR\s*#\s*(\d+)\b", re.I)
SHA_FIND_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.I)
TASK_REF_RE = re.compile(r"\btask\s*#?\s*(\d+)\b", re.I)
DONE_NOTE_RE = re.compile(r"^Task #(\d+) is done\b", re.I)


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")
