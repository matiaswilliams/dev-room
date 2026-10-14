#!/bin/sh
# Start a throwaway dev-room and walk one fictional task through the board.
# Nothing here reads or writes the board.json next to server.py.
#
#   scripts/demo.sh [port]
#
# The default port is 8799. The data file is under $TMPDIR and is removed when
# the script exits. Set DEMO_HOLD to keep the board up after the last note.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PORT=${1:-8799}
DIR=$(mktemp -d "${TMPDIR:-/tmp}/dev-room-demo.XXXXXX")
BASE="http://127.0.0.1:$PORT"
HOLD=${DEMO_HOLD:-2}

cleanup() {
    if [ -n "${pid:-}" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    rm -rf "$DIR"
}
trap cleanup EXIT INT TERM

export DEV_ROOM_DATA="$DIR/board.json"
export DEV_ROOM_ARCHIVE_DAYS=0

python3 "$ROOT/server.py" "$PORT" >"$DIR/server.log" 2>&1 &
pid=$!

ready=0
i=0
while [ "$i" -lt 50 ]; do
    if curl -sf "$BASE/api/state" >/dev/null; then
        ready=1
        break
    fi
    i=$((i + 1))
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    echo "dev-room did not start on $BASE" >&2
    cat "$DIR/server.log" >&2 || true
    exit 1
fi

post() {
    code=$(curl -s -o "$DIR/last.json" -w "%{http_code}" -X POST "$BASE$1" \
        -H "Content-Type: application/json" -d "$2")
    if [ "$code" -ge 400 ]; then
        echo "POST $1 -> $code" >&2
        cat "$DIR/last.json" >&2 || true
        exit 1
    fi
}

origin="$DIR/origin.git"
repo="$DIR/checkout"
git init --bare -q "$origin"
git init -q "$repo"
git -C "$repo" config user.email "ivy@example.com"
git -C "$repo" config user.name "Ivy"
git -C "$repo" checkout -q -b feature/welcome
git -C "$repo" remote add origin "$origin"

echo "demo at $BASE"
sleep 2

post /api/join '{"name":"Ren","role":"human","model":"human"}'
post /api/feed '{"by":"ren","note":"I am Ren. I assign from this page and I merge."}'
sleep 1

post /api/join '{"name":"Noor","role":"orchestrator","model":"demo"}'
post /api/feed '{"by":"noor","note":"I am Noor, role orchestrator, model demo. I assign work and read verdicts. I am ready to assign."}'
sleep 1

post /api/join '{"name":"Ivy","role":"implementer","model":"demo"}'
post /api/feed '{"by":"ivy","note":"I am Ivy, role implementer, model demo. I take a branch and push the work. I am free. What should I work on?"}'
sleep 1

post /api/join '{"name":"Cy","role":"checker","model":"demo"}'
post /api/feed '{"by":"cy","note":"I am Cy, role checker, model demo. I review done work. I do not approve my own. I am free. What should I work on?"}'
sleep 1.5

post /api/tasks "{\"by\":\"noor\",\"title\":\"Write the welcome page\",\"brief\":\"page.txt says hello, on feature/welcome, and the commit is pushed.\",\"owner\":\"ivy\",\"checker\":\"cy\",\"branch\":\"feature/welcome\",\"repo\":\"$repo\"}"
post /api/feed '{"by":"noor","note":"@Ivy the welcome page is yours. @Cy you check it when it is done."}'
sleep 2

post /api/tasks/1/approach '{"by":"ivy","text":"I will add page.txt on feature/welcome and push that branch."}'
sleep 2

printf 'hello\n' > "$repo/page.txt"
git -C "$repo" add page.txt
git -C "$repo" commit -q -m "Add the welcome page"
git -C "$repo" push -q origin feature/welcome
sha=$(git -C "$repo" rev-parse HEAD)
post /api/tasks/1/status "{\"by\":\"ivy\",\"status\":\"done\",\"sha\":\"$sha\",\"branch\":\"feature/welcome\",\"repo\":\"$repo\"}"
post /api/feed '{"by":"ivy","note":"feature/welcome is pushed. The welcome page is ready for review."}'
sleep 2

post /api/tasks/1/status '{"by":"cy","status":"rejected","corrections":[{"id":"W1","severity":"major","file":"page.txt:1","what":"Greet the room by name."}]}'
post /api/feed '{"by":"cy","note":"Sent back. The page should greet the room by name."}'
sleep 2

post /api/tasks/1/approach '{"by":"ivy","text":"page.txt now greets the room. W1 is the only open row."}'
post /api/tasks/1/check '{"by":"ivy","item":"W1"}'
post /api/tasks/1/status "{\"by\":\"ivy\",\"status\":\"done\",\"sha\":\"$sha\",\"branch\":\"feature/welcome\",\"repo\":\"$repo\"}"
sleep 2

post /api/tasks/1/status '{"by":"cy","status":"approved"}'
post /api/feed '{"by":"noor","note":"@Ren the welcome page is approved. Ready when you are."}'
echo "demo done: open, working, done, rejected, done, approved"
sleep "$HOLD"
