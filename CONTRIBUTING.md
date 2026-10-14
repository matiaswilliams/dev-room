# Contributing

## Setup

Fork the repository with the Fork button, then:

```bash
git clone https://github.com/YOUR_NAME/dev-room.git
cd dev-room
python3 server.py
```

You need Python 3.11 or newer, and Git. There is no package to install.

Open [http://127.0.0.1:8791](http://127.0.0.1:8791) and try a join from the README. Your `board.json` is local. Leave it untracked.

## Check a change

Stop anything you started on ports 8871 and 8872. The loop test uses those ports for a fresh board.

```bash
python3 tests/test_rules.py
python3 tests/test_public_loop.py
```

Each passing run prints `all passed`. The tests create their own data files and a throwaway git origin. They do not read or write your `board.json`. A change to a board rule needs a check in `tests/test_rules.py`.

## Leave these out of the commit

Do not commit `board.json`, `board.json.tmp`, `board-archive/`, `.env`, or log files. `.gitignore` already excludes them. A pull request that adds a machine's board history will be closed.

## Pull request

Branch from `main`, make the change, run the tests, and open a pull request. Say what changed and that both tests printed `all passed`. GitHub Actions runs them again on Python 3.11, 3.12, and 3.13.

The license is MIT. By opening a pull request you agree that your change is released under that license.
