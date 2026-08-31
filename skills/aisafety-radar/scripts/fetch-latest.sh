#!/usr/bin/env bash
# Print the exact bytes of one published file or bundle after verifying its checksum.
# Usage: fetch-latest.sh today.md | week.md | careers.md | map.md | 31-today-forums.md
set -euo pipefail
name="${1:?usage: fetch-latest.sh <file-or-bundle>}"
base="https://mickzijdel.github.io/aisafety-radar/latest"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsS --proto '=https' --max-redirs 0 --max-time 30 -o "$tmp" "$base/$name"
# checksums.txt is `sha256sum` output: "<64 hex>  <name>", two spaces, one file per line.
expected="$(curl -fsS --proto '=https' --max-redirs 0 --max-time 30 "$base/checksums.txt" | awk -v n="$name" '$2 == n {print $1}')"
if [ -z "$expected" ]; then
  echo "fetch-latest: no checksum published for $name" >&2
  exit 1
fi
actual="$(sha256sum "$tmp" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
  echo "fetch-latest: checksum mismatch for $name" >&2
  exit 1
fi
cat "$tmp"
