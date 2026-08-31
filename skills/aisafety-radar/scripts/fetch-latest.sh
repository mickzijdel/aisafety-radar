#!/usr/bin/env bash
# Print the exact bytes of one published file or bundle after verifying its checksum.
# Usage: fetch-latest.sh today.md | week.md | careers.md | map.md | 31-today-forums.md
#
# Two failure classes, two exit codes, because the caller's next move differs:
#   1  the site could not be reached (DNS, TLS, 404, timeout) - retrying by another route is
#      reasonable, so SKILL.md sends the agent to the URL-fetch tool.
#   2  the file was fetched but its integrity could not be established: no checksum is published
#      for it, or the checksum does not match. Nothing was printed, and nothing about this file
#      should be trusted - SKILL.md sends the agent to the bundled copy and tells it to say so.
set -euo pipefail
name="${1:?usage: fetch-latest.sh <file-or-bundle>}"
base="https://mickzijdel.github.io/aisafety-radar/latest"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if ! curl -fsS --proto '=https' --max-redirs 0 --max-time 30 -o "$tmp" "$base/$name"; then
  echo "fetch-latest: could not fetch $name" >&2
  exit 1
fi
if ! sums="$(curl -fsS --proto '=https' --max-redirs 0 --max-time 30 "$base/checksums.txt")"; then
  echo "fetch-latest: could not fetch checksums.txt" >&2
  exit 1
fi
# checksums.txt is `sha256sum` output: "<64 hex>  <name>", two spaces, one file per line.
expected="$(printf '%s\n' "$sums" | awk -v n="$name" '$2 == n {print $1}')"
if [ -z "$expected" ]; then
  echo "fetch-latest: no checksum published for $name" >&2
  exit 2
fi
actual="$(sha256sum "$tmp" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
  echo "fetch-latest: checksum mismatch for $name" >&2
  exit 2
fi
cat "$tmp"
