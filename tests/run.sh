#!/usr/bin/env bash
# tests/run.sh - run every tests/*.test.sh; exit 1 if any fails.
set -u
cd "$(dirname "$0")" || exit 1
rc=0
for t in ./*.test.sh; do
  bash "$t" || rc=1
done
exit "$rc"
