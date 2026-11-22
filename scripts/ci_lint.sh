#!/bin/bash
# Fast checks that need no Python packages; CI runs this on every push and pull request
# (.github/workflows/ci.yml), and it runs the same way in WSL:  scripts/ci_lint.sh
#
#   1. no CRLF line endings (a Windows edit breaks shell scripts in Linux)
#   2. shellcheck on every shell script, found by extension or by its #! line
#   3. every Python file compiles; every JSON and TOML file parses
#   4. desktop-file-validate on the .desktop launchers
#
# Needs git, python3, shellcheck and desktop-file-utils. Outside CI a missing tool is skipped
# with a note; in CI (CI=true) it is an error.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
step() { printf '\n== %s\n' "$1"; }
bad() { echo "FAIL: $*"; fail=1; }
need() {
    command -v "$1" >/dev/null && return 0
    if [ "${CI:-}" = true ]; then bad "$1 is not installed"; else echo "skipped: $1 is not installed"; fi
    return 1
}

# Every check below lists files with git; if git can't read the repository, stop rather than
# "pass" on an empty list.
if ! tracked=$(git ls-files | wc -l) || [ "$tracked" -eq 0 ]; then
    echo "FAIL: git ls-files lists no files (not a git checkout, or git refuses it)"
    exit 1
fi
echo "$tracked tracked files"

step "Line endings"
crlf=$(git ls-files --eol | awk '$1 ~ /crlf|mixed/ || $2 ~ /crlf|mixed/ { sub(/^[^\t]*\t/, ""); print }')
if [ -n "$crlf" ]; then bad "CRLF line endings in:"; echo "$crlf"; else echo "ok"; fi

step "Shell scripts"
mapfile -t shells < <(git ls-files | while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
        *.sh) echo "$f" ;;
        *.py|*.js|*.json|*.md|*.png|*.svg|*.webp|*.wav|*.gguf|*.onnx|*.bin) ;;
        *) head -n1 "$f" 2>/dev/null | grep -qE '^#!.*[/ ](ba|da)?sh([[:space:]]|$)' && echo "$f" ;;
    esac
done)
echo "${#shells[@]} scripts"
if need shellcheck; then
    shellcheck -S warning "${shells[@]}" || bad "shellcheck found problems"
fi

step "Python, JSON and TOML"
git ls-files -z '*.py' '*.json' '*.toml' | python3 -c '
import json, sys, tomllib
bad = 0
for name in sys.stdin.read().split("\0"):
    if not name:
        continue
    try:
        with open(name, "rb") as f:
            data = f.read()
        if name.endswith(".py"):
            compile(data, name, "exec")
        elif name.endswith(".json"):
            json.loads(data)
        else:
            tomllib.loads(data.decode())
    except Exception as e:
        print(f"{name}: {e}")
        bad = 1
sys.exit(bad)
' && echo "ok" || bad "a file does not compile or parse"

step "Desktop launchers"
if need desktop-file-validate; then
    # Two files are KDE formats, not freedesktop desktop entries, so the validator rejects them:
    # Dolphin's right-click service menu (Type=Service) and the Recycle Bin link (KDE's EmptyIcon).
    mapfile -t desktops < <(git ls-files '*.desktop' | grep -v -e '/servicemenus/' -e '/recycle-bin.desktop$')
    echo "${#desktops[@]} files"
    desktop-file-validate --no-hints "${desktops[@]}" || bad "desktop-file-validate found problems"
fi

echo
if [ "$fail" = 0 ]; then echo "All checks passed."; else echo "Some checks failed."; fi
exit "$fail"
