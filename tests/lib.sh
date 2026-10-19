# shellcheck shell=bash
# tests/lib.sh - shared helpers: ROOT, tmproot, pass, fail.
# shellcheck disable=SC2034  # used by the test scripts
ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEST_NAME=$(basename "$0" .test.sh)

tmproot() { # <label>: set TMP to a private temp folder removed when the test exits
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/tokenrate-$1.XXXXXX") || exit 1
  # shellcheck disable=SC2064
  trap "rm -rf '$TMP'" EXIT
}

pass() { printf 'ok   %s: %s\n' "$TEST_NAME" "$*"; }
fail() { printf 'FAIL %s: %s\n' "$TEST_NAME" "$*" >&2; exit 1; }
