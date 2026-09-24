#!/usr/bin/env bash
# Enable the JSONL board union merge driver in THIS clone.
#
# git merge drivers are configured per repository (`.gitattributes` selects the
# driver name, but the command behind it lives in git config, which is NOT
# cloned). Run this once after cloning, and in every fleet tree that appends to
# a .coding-hermes board.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
driver="$repo/scripts/board-merge-driver.py"

[ -f "$driver" ] || { echo "missing $driver" >&2; exit 1; }
chmod +x "$driver"

cd "$repo"
git config merge.boardjsonl.name "union JSONL board merge (no row lost, ids kept unique)"
git config merge.boardjsonl.driver "python3 $driver %O %A %B"

echo "board merge driver enabled in $repo:"
echo "  merge.boardjsonl.driver = $(git config --get merge.boardjsonl.driver)"
grep -q 'board/\*.jsonl merge=boardjsonl' .gitattributes 2>/dev/null \
  && echo "  .gitattributes selects merge=boardjsonl for .coding-hermes/board/*.jsonl" \
  || echo "  WARNING: .gitattributes does not select the driver — merges will still conflict" >&2
