#!/usr/bin/env bash
# Assert that every place a toolchain or service version is pinned agrees.
#
# A version gets spelled out in several files because different consumers read different ones:
# mise.toml drives local dev (and mise-action in CI), .ruby-version / .node-version /
# .python-version feed setup-ruby / setup-node / setup-python, the Dockerfile ARGs build the
# production image, package.json's `packageManager` field drives corepack, and the `image:` tags
# in a compose file or config/deploy.yml decide what production runs versus what CI tests
# against. Nothing makes them agree on its own, so a bump that misses one file is silent: the
# image builds on a different Ruby than the tests ran on, or the suite goes green against a
# database server nobody deploys.
#
# Part of the dev-env standard (dev-hooks:dev-env-setup, v24) — run by the hk `versions` step and
# CI's `versions` job so the local and CI gates can't drift. Don't hand-edit the logic; the next
# policy change should be a plain re-copy of the template (a repo's own formatter may re-indent
# this file to local style, which is fine).
#
# Only files that exist get checked, and every skip is printed: a repo legitimately without a
# Dockerfile or a compose file passes, but its pass never looks like more coverage than it is.
# The gate reports and never rewrites a pin — which file holds the correct value is a judgement
# call (in one repo the right fix was to change production, not CI).
#
# Deliberately NOT checked: go.mod's `go` directive. It declares the *minimum* language version
# the module builds with, not the toolchain a build pins, so it is routinely — and correctly —
# older than mise.toml's `go`. Comparing them would fail healthy repos.
#
# Deliberately NOT enforced: Dockerfile style. Whether an image hardcodes `ARG NODE_VERSION` or
# derives the Node major from .node-version is a per-repo choice. This verifies that whatever
# pins exist agree, so adopting the standard never forces a Dockerfile rewrite.
#
# EVERY Dockerfile in the repo root is checked, not just the first one found. A repo
# commonly carries a production `Dockerfile` beside a `Dockerfile.dev`, and stopping at the first
# is how one of them sat on node:22 for months while .node-version, mise.toml and the production
# Dockerfile all said 24 — with this gate green the whole time, in the repo whose own docs claimed
# Node 24 "everywhere".

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0
note() {
  echo "  ✗ $1"
  fail=1
}
skip() { echo "  - $1"; }

TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT

# ── Toolchain pins ────────────────────────────────────────────────────────────────────
MISE=""
for f in mise.toml .mise.toml; do
  if [ -f "$f" ]; then
    MISE=$f
    break
  fi
done

# Newline-separated, because a `for f in $DOCKERFILES` would word-split a name containing a
# space. An unmatched glob is left literal by the shell (no nullglob here), so `Dockerfile.*` in
# a repo with only a plain `Dockerfile` survives as that literal string — the `[ -f ]` guard is
# what drops it, and must not be removed. `Dockerfile` itself needs the literal dot to match
# `Dockerfile.*`, so it can never be listed twice.
#
# Excluded: editor/VCS leftovers (`Dockerfile.dev.bak`, `Dockerfile.orig`) and templates
# (`Dockerfile.j2`) — neither is a build input, and a template's `ARG NODE_VERSION={{ ... }}`
# would fail forever with no correct value to change it to.
DOCKERFILES=""
for f in Dockerfile Containerfile Dockerfile.* Containerfile.*; do
  [ -f "$f" ] || continue
  case $f in
    *.bak | *.orig | *.rej | *.save | *.swp | *.swo | *.tmp | *.disabled | *~) continue ;;
    *.example | *.sample | *.j2 | *.tpl | *.template | *.erb) continue ;;
  esac
  DOCKERFILES="$DOCKERFILES$f
"
done

# A mise.toml `[tools]` value. Handles both `node = "22.4.1"` and the table form
# `ruby = { version = "4.0.2", compile = false }` by taking the first quoted string after `=`.
read_mise() {
  [ -n "$MISE" ] || return 0
  awk -v key="$1" '
    /^[[:space:]]*\[/ { intools = ($0 ~ /^[[:space:]]*\[tools\][[:space:]]*$/); next }
    !intools { next }
    {
      line = $0
      sub(/#.*/, "", line)
      if (line !~ "^[[:space:]]*\"?" key "\"?[[:space:]]*=") next
      sub(/^[^=]*=/, "", line)
      if (match(line, /"[^"]*"/)) { print substr(line, RSTART + 1, RLENGTH - 2); exit }
    }
  ' "$MISE"
}

# One Dockerfile's `ARG NAME=value` defaults, one per line, deduplicated. A multi-stage build may
# redeclare a bare `ARG NAME` to pull it into a later stage's scope; those carry no pin, so only
# `=` lines count. $1 is the file, $2 the ARG name.
#
# Strips blanks, CR and quotes but NOT newlines: `[:space:]` here used to delete the line
# separators too, which collapsed two differing defaults into one line, so `lines` could only
# ever answer 0 or 1 and the caller's "conflicting defaults" branch was unreachable. A file
# declaring 24.19.0 and 20.0.0 reported the value as "24.19.020.0.0" instead.
read_arg() {
  sed -n "s/^[[:space:]]*ARG[[:space:]]\{1,\}$2=//p" "$1" | tr -d "[:blank:]\r\"'" | sort -u
}

# package.json's `"packageManager": "pnpm@9.1.0+sha512…"` — corepack's pin, for the JS stack.
read_pkgmgr() {
  [ -f package.json ] || return 0
  sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' package.json |
    head -n1 | awk -F'@' -v t="$1" 'NF > 1 && $1 == t { sub(/\+.*/, "", $2); print $2 }'
}

# `.ruby-version` may be bare (3.4.10) or prefixed (ruby-3.4.10); `.node-version` may carry a
# leading v (v22.4.1). Every form is valid for setup-* and mise, so normalise before comparing.
normalize() {
  local v
  v=$(printf '%s' "$2" | tr -d "[:space:]\"'")
  v=${v#"$1"-}
  v=${v#"$1"}
  case $v in v[0-9]*) v=${v#v} ;; esac
  printf '%s' "$v"
}

lines() { printf '%s' "$1" | grep -c .; }

n=0
first_src=""
first_ver=""
all_srcs=""
mismatch=0
add_source() { # $1 label, $2 version
  n=$((n + 1))
  all_srcs="${all_srcs:+$all_srcs, }$1"
  if [ "$n" -eq 1 ]; then
    first_src=$1
    first_ver=$2
  elif [ "$2" != "$first_ver" ]; then
    note "$1 ($2) != $first_src ($first_ver)"
    mismatch=1
  fi
}

echo "Toolchain:"
[ -n "$MISE" ] || skip "no mise.toml, so no mise pins to cross-check"
[ -n "$DOCKERFILES" ] || skip "no Dockerfile, so no image-build ARGs to cross-check"

# tool | version file (empty = no conventional one) | Dockerfile ARG
while IFS='|' read -r tool vfile arg; do
  [ -n "$tool" ] || continue
  n=0
  first_src=""
  first_ver=""
  all_srcs=""
  mismatch=0
  floating=""

  if [ -n "$vfile" ] && [ -f "$vfile" ]; then
    ver=$(normalize "$tool" "$(cat "$vfile")")
    [ -n "$ver" ] && add_source "$vfile" "$ver"
  fi

  raw=$(read_mise "$tool")
  if [ -n "$raw" ]; then
    # "latest"/"lts" and backend-prefixed specs (aqua:…, ruby-build:…) name no fixed version —
    # mise.lock is their real pin — so there is nothing to compare a version file against.
    case $raw in
      *:*) floating=$raw ;;
      *[0-9]*) add_source "$MISE $tool" "$(normalize "$tool" "$raw")" ;;
      *) floating=$raw ;;
    esac
  fi

  raw=$(read_pkgmgr "$tool")
  [ -n "$raw" ] && add_source "package.json packageManager" "$(normalize "$tool" "$raw")"

  # Each Dockerfile is its own source, labelled by name: with two of them the ✓ line lists both,
  # and a mismatch says which file to fix rather than just "Dockerfile".
  while IFS= read -r dockerfile; do
    [ -n "$dockerfile" ] || continue
    raw=$(read_arg "$dockerfile" "$arg")
    case "$(lines "$raw")" in
      0) ;;
      1) add_source "$dockerfile ARG $arg" "$(normalize "$tool" "$raw")" ;;
      *) note "$dockerfile declares ARG $arg with conflicting defaults: $(printf '%s' "$raw" | tr '\n' ' ')" ;;
    esac
  done <<<"$DOCKERFILES"

  # A floating mise spec is only worth mentioning when some other file does pin the tool —
  # on its own it is the standard's normal state, not a gap.
  floating_note=""
  [ -n "$floating" ] && floating_note=" ($MISE spec is \"$floating\", no fixed version to compare)"

  case $n in
    0) ;; # this repo pins the tool nowhere — nothing to say about it
    1) skip "$tool: pinned only in $first_src ($first_ver)$floating_note, nothing to cross-check" ;;
    *)
      [ -n "$floating" ] && skip "$tool: $MISE spec is \"$floating\" (no fixed version), not compared"
      [ "$mismatch" = 0 ] && echo "  ✓ $tool $first_ver — $all_srcs"
      ;;
  esac
done <<'TOOLS'
ruby|.ruby-version|RUBY_VERSION
node|.node-version|NODE_VERSION
python|.python-version|PYTHON_VERSION
go|.go-version|GO_VERSION
yarn||YARN_VERSION
pnpm||PNPM_VERSION
npm||NPM_VERSION
bun||BUN_VERSION
TOOLS

# ── Service image tags ────────────────────────────────────────────────────────────────
# CI has to exercise the services production actually runs, or the suite goes green against a
# database nobody deploys. Deployment manifests differ per repo (a compose file, Kamal's
# config/deploy.yml, a devcontainer compose), so discover whichever are present instead of
# hardcoding one, and compare every file that pins a given image against the others. An image
# named in only one file is not drift (CI may legitimately not need Redis), so it is reported
# but never fails.
echo
echo "Service image tags:"

FILES=""
nfiles=0
for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml \
  config/deploy.yml config/deploy.yaml \
  .devcontainer/compose.yml .devcontainer/compose.yaml \
  .devcontainer/docker-compose.yml .devcontainer/docker-compose.yaml \
  .github/workflows/*.yml .github/workflows/*.yaml; do
  if [ -f "$f" ]; then
    FILES="$FILES$f
"
    nfiles=$((nfiles + 1))
  fi
done

if [ "$nfiles" -eq 0 ]; then
  skip "no compose / deploy / workflow files, nothing to cross-check"
else
  # `image: mysql:8.4`, `image: "mysql:8.4"`, `image: mysql:8.4@sha256:…` (CI pins by digest, so
  # match only the tag). Commented-out and templated (${…}, {{…}}, <%…%>) images are skipped, as
  # are untagged ones (a bare `image: acme/app` pins nothing).
  printf '%s' "$FILES" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    awk -v f="$f" '
      {
        line = $0
        sub(/#.*/, "", line)
        if (line !~ /^[[:space:]]*-?[[:space:]]*image:[[:space:]]*[^[:space:]]/) next
        sub(/^[[:space:]]*-?[[:space:]]*image:[[:space:]]*/, "", line)
        gsub(/["\047]/, "", line)
        sub(/[[:space:]].*$/, "", line)
        sub(/@.*$/, "", line)
        if (line ~ /\$|\{\{|<%/) next
        nc = split(line, part, ":")
        if (nc < 2) next
        tag = part[nc]
        repo = substr(line, 1, length(line) - length(tag) - 1)
        if (repo == "" || tag == "") next
        sub(/^docker\.io\//, "", repo)
        sub(/^library\//, "", repo)
        print repo "\t" tag "\t" f
      }
    ' "$f"
  done | sort -u >"$TMP"

  if [ ! -s "$TMP" ]; then
    skip "no tagged \`image:\` pins in the $nfiles compose/deploy/workflow file(s) present"
  elif ! awk -F'\t' '
    {
      if (!($1 in seen)) { seen[$1] = 1; order[++nk] = $1 }
      fkey = $1 SUBSEP $3
      if (!(fkey in fseen)) { fseen[fkey] = 1; nf[$1]++; files[$1] = files[$1] (files[$1] ? ", " : "") $3 }
      tkey = $1 SUBSEP $2
      if (!(tkey in tseen)) { tseen[tkey] = 1; nt[$1]++ }
      onefile[$1] = $3
      onetag[$1] = $2
      detail[$1] = detail[$1] (detail[$1] ? ", " : "") $3 " (" $2 ")"
    }
    END {
      bad = 0
      for (i = 1; i <= nk; i++) {
        k = order[i]
        if (nf[k] < 2)
          printf "  - %s: pinned only in %s (%s), nothing to cross-check\n", k, onefile[k], onetag[k]
        else if (nt[k] == 1)
          printf "  ✓ %s %s — %s\n", k, onetag[k], files[k]
        else {
          printf "  ✗ %s tags disagree: %s\n", k, detail[k]
          bad = 1
        }
      }
      if (bad) print "    CI must exercise the services production runs — pick the right value and set it everywhere."
      exit bad
    }
  ' "$TMP"; then
    fail=1
  fi
fi

[ "$fail" -eq 0 ] || echo "Version pins disagree — fix the file(s) named above."
exit "$fail"
