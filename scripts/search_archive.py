#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Search board-archive for one word in a task title or a feed note.

Does not read board.json.

  python3 scripts/search_archive.py WORD [--dir DIR]

A hit prints `kind id`. A miss prints nothing.
"""

import json
import os
import sys


def default_dir() -> str:
    data = os.environ.get("DEV_ROOM_DATA") or os.environ.get("AGENT_BOARD_DATA")
    if not data:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data = os.path.join(root, "board.json")
    return os.path.join(os.path.dirname(os.path.abspath(data)), "board-archive")


def search(folder: str, word: str) -> list[tuple[str, object]]:
    """Kind and item id for each line whose title or note contains word."""
    needle = word.casefold()
    if not needle or not os.path.isdir(folder):
        return []
    hits: list[tuple[str, object]] = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = row.get("kind")
                item = row.get("item") if isinstance(row.get("item"), dict) else None
                if not item or kind not in {"task", "post"}:
                    continue
                text = item.get("title") if kind == "task" else item.get("note")
                if needle in str(text or "").casefold():
                    hits.append((kind, item.get("id")))
    return hits


def main(argv: list[str]) -> int:
    folder = ""
    words: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--dir":
            if index + 1 >= len(argv):
                print("pass a directory after --dir", file=sys.stderr)
                return 2
            folder = argv[index + 1]
            index += 2
            continue
        if arg.startswith("-"):
            print(f"unknown argument {arg}", file=sys.stderr)
            return 2
        words.append(arg)
        index += 1
    if len(words) != 1 or not words[0].strip():
        print("pass one word to search for", file=sys.stderr)
        return 2
    for kind, item_id in search(folder or default_dir(), words[0]):
        print(f"{kind} {item_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
