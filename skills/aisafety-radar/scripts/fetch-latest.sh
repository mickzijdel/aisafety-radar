#!/usr/bin/env bash
# Print the exact bytes of one published file or bundle after verifying its checksum.
# Usage: fetch-latest.sh today.md | week.md | careers.md | map.md | 31-today-forums.md
#
# Two failure classes, two exit codes, because the caller's next move differs:
#   1  the file itself could not be fetched (DNS, TLS, 404, timeout). Nothing was downloaded, so
#      nothing is in question - retrying by another route is reasonable, and SKILL.md sends the
#      agent to the URL-fetch tool.
#   2  the file was fetched but its integrity could not be established: the checksum list could
#      not be fetched, no checksum is published for this file, or the checksum does not match.
#      Nothing was printed, and nothing about this file should be trusted - SKILL.md sends the
#      agent to the bundled copy and tells it to say so.
#
# The line between them is whether the file arrived, not what kind of error occurred. Once these
# bytes are in hand, failing to fetch the list that would vouch for them is integrity-indeterminate
# and nothing else: exit 1 there would send the agent to fetch the same unverifiable bytes by
# another route, with nothing checking them at the far end, and would claim the site is
# unreachable when it answered for the file a moment earlier.
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
  # Exit 2, not 1: $name is downloaded and unverifiable, which is what exit 2 means.
  echo "fetch-latest: could not fetch checksums.txt; cannot verify $name" >&2
  exit 2
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
