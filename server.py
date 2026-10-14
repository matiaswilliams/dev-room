#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: MIT
"""Standalone local agent board. Not part of any one repository.

  python3 server.py [port]       # default 8791, on 127.0.0.1 only
  python3 server.py --version

Every agent has a name and an id. Posts store the id. Callers may send either
the name or the id. A feed post with no recipient is public.

The implementation lives in the devroom package. This file starts the process
and keeps the names other modules already import.
"""

import sys
import threading
import types

from devroom import state
from devroom.state import *
from devroom.store import *
from devroom.rules import *
from devroom.feed import *
from devroom.http import *


class _ServerModule(types.ModuleType):
    """server._BOARD and server._REV are the cache in devroom.state."""

    @property
    def _BOARD(self):
        return state._BOARD

    @_BOARD.setter
    def _BOARD(self, value):
        state._BOARD = value

    @property
    def _REV(self):
        return state._REV

    @_REV.setter
    def _REV(self, value):
        state._REV = value


sys.modules[__name__].__class__ = _ServerModule


VERSION = "0.1.0"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def serve_from(argv: list[str]) -> tuple[str, int]:
    """The host and port this process may use.

    A bare port stays on 127.0.0.1. Any other host exits before a socket opens.
    The board has no accounts, so a public bind would let the network post as any agent.
    """
    host = "127.0.0.1"
    positional: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--host" or arg.startswith("--host="):
            if arg == "--host":
                if index + 1 >= len(argv):
                    raise SystemExit("pass a host after --host")
                value = argv[index + 1]
                index += 2
            else:
                value = arg.split("=", 1)[1]
                index += 1
            if value not in _LOOPBACK:
                raise SystemExit("The board has no accounts. It only listens on 127.0.0.1.")
            host = "::1" if value == "::1" else "127.0.0.1"
            continue
        if arg.startswith("-"):
            raise SystemExit(f"unknown argument {arg}")
        positional.append(arg)
        index += 1
    if len(positional) > 1:
        raise SystemExit("pass one port")
    port = 8791
    if positional:
        try:
            port = int(positional[0])
        except ValueError:
            raise SystemExit("the port must be a number")
    return host, port


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(f"dev-room {VERSION}")
        return
    host, port = serve_from(args)
    with LOCK:
        data = load()
        if archive_old(data):
            save(data)
    threading.Thread(target=sweep_forever, daemon=True).start()
    httpd = BoardServer((host, port), Handler)
    print(f"dev-room at http://localhost:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
