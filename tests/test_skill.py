"""The shipped skill package: frontmatter, body limits, manifests and the fetch script.

Everything here reads the files a user installs, not the pipeline that regenerates them, so
the assertions are deliberately about bytes on disk: an agent runtime parses SKILL.md's
frontmatter itself and will reject keys the spec does not define.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from pipeline.render import BUNDLES, PAGES_BASE, PackMeta

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "aisafety-radar" / "SKILL.md"
SCRIPT = ROOT / "skills" / "aisafety-radar" / "scripts" / "fetch-latest.sh"
INDEX = ROOT / "skills" / "aisafety-radar" / "references" / "00-index.md"
ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def frontmatter():
    text = SKILL.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    return yaml.safe_load(m.group(1)), text


def test_frontmatter_is_spec_only_and_named_after_directory():
    fm, _ = frontmatter()
    assert set(fm) <= ALLOWED, set(fm) - ALLOWED
    assert fm["name"] == SKILL.parent.name == "aisafety-radar"
    assert 50 < len(fm["description"]) <= 1024
    assert "version" not in fm.get("metadata", {})


def test_skill_body_limits_and_style():
    _, text = frontmatter()
    assert len(text.splitlines()) < 200
    assert "—" not in text
    for needed in ("00-index.md", "fetch-latest.sh", "48", "90-live-sources.md", "untrusted"):
        assert needed in text, needed


def test_published_urls_agree_with_the_pipeline_base():
    # The header the pipeline writes into every appendix, the URL the skill tells an agent to
    # fetch, the URL the script actually fetches, and the placeholder index's own header all
    # name the same host AND the same `latest` directory. Four copies of one string, so the
    # test reads PackMeta's default rather than repeating it: a base that resolves to the
    # right host but the wrong directory 404s exactly as loudly as a wrong host.
    latest = PackMeta.__dataclass_fields__["latest_base"].default
    assert latest == f"{PAGES_BASE}/latest"
    fm, _ = frontmatter()
    assert fm["metadata"]["latest"].startswith(latest)
    base_line = next(line for line in SCRIPT.read_text().splitlines() if line.startswith("base="))
    assert base_line.split("=", 1)[1].strip('"') == latest
    assert f"{latest}/00-index.md" in INDEX.read_text()


def test_fetch_script_verifies_a_checksum_before_printing():
    script = SCRIPT.read_text()
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert "checksums.txt" in script
    assert "sha256sum" in script
    # Two failure classes, two exit codes, split on whether the file arrived: 1 only for "the
    # file itself could not be fetched", 2 for every way "fetched it but cannot vouch for it"
    # happens (no checksum list, no checksum for this file, or a mismatch). SKILL.md routes the
    # agent differently on each, so the two must not share a code. Count statements rather than
    # substrings: the header comment names both codes while explaining where the line between
    # them falls, and a comment is not control flow.
    exits = [line.strip() for line in script.splitlines() if line.strip().startswith("exit ")]
    assert exits.count("exit 1") == 1, exits
    assert exits.count("exit 2") == 3, exits
    assert script.index("sha256sum") < script.index("cat ")


# A stand-in `curl` on PATH, so the script's real control flow can be executed without a network.
# It answers the two URLs the script fetches out of the environment, and fails the way curl fails
# when `FAKE_CURL_FAIL` names which one to break.
FAKE_CURL = """#!/usr/bin/env bash
url="${@: -1}"
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
case "$url" in
  */checksums.txt)
    if [ "${FAKE_CURL_FAIL:-}" = "sums" ]; then exit 22; fi
    printf '%s\\n' "$FAKE_SUMS"
    ;;
  *)
    if [ "${FAKE_CURL_FAIL:-}" = "body" ]; then exit 22; fi
    printf '%s' "$FAKE_BODY" > "$out"
    ;;
esac
"""

BODY = "<!-- aisafety-radar -->\nreal bytes\n"
BODY_SHA = hashlib.sha256(BODY.encode()).hexdigest()


def run_fetch(tmp_path, sums, fail=""):
    """Run the real script with a stub curl, and return (exit code, stdout)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "curl").write_text(FAKE_CURL)
    (fake / "curl").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake}:{os.environ['PATH']}",
        "FAKE_BODY": BODY,
        "FAKE_SUMS": sums,
        "FAKE_CURL_FAIL": fail,
    }
    done = subprocess.run(
        ["bash", str(SCRIPT), "31-today-forums.md"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout


def test_the_fetch_script_prints_the_body_when_the_checksum_matches(tmp_path):
    code, out = run_fetch(tmp_path, f"{BODY_SHA}  31-today-forums.md")
    assert code == 0 and out == BODY


@pytest.mark.parametrize(
    ("label", "sums"),
    [
        ("checksum mismatch", f"{'0' * 64}  31-today-forums.md"),
        ("no checksum for this file", f"{BODY_SHA}  week.md"),
        ("empty checksums file", ""),
    ],
)
def test_the_fetch_script_exits_2_and_prints_nothing_on_an_integrity_failure(label, sums, tmp_path):
    code, out = run_fetch(tmp_path, sums)
    assert (code, out) == (2, ""), label


def test_a_missing_checksums_file_is_an_integrity_failure_not_a_transport_one(tmp_path):
    """The file downloaded; the list that would vouch for it did not. That is precisely the state
    exit 2 names, and it used to exit 1 -- which sends the agent to the URL-fetch tool to get the
    same unverifiable bytes by another route, with nothing checking them at the far end. "Could
    not reach the site" is also simply false: the site answered for the file a moment earlier."""
    code, out = run_fetch(tmp_path, f"{BODY_SHA}  31-today-forums.md", fail="sums")
    assert (code, out) == (2, "")


def test_the_fetch_script_exits_1_when_the_file_itself_cannot_be_fetched(tmp_path):
    # Nothing was downloaded, so there is nothing whose integrity is in question. This is the one
    # worth retrying by another route, and SKILL.md sends the agent to do exactly that.
    code, out = run_fetch(tmp_path, f"{BODY_SHA}  31-today-forums.md", fail="body")
    assert (code, out) == (1, "")


def test_the_skill_routes_the_two_exit_codes_differently():
    _, text = frontmatter()
    assert "exit 2" in text and "exit 1" in text
    # The bundled copy is for the file we cannot vouch for; the fetch tool is for the one we
    # could not reach. Getting these the wrong way round publishes an unverified file.
    integrity, transport = text.index("exit 2"), text.index("exit 1")
    assert "bundled copy" in text[integrity : integrity + 200]
    assert "URL-fetch" in text[transport : transport + 200]


def _names(bundle: str, text: str) -> bool:
    """Does `text` name `bundle` as itself, rather than as the tail of an appendix name?

    `21-this-week.md` ends in `week.md` and `15-careers.md` ends in `careers.md`, so a plain
    substring search reads the recipe list as an advertisement for two bundles it never mentions.
    """
    return re.search(rf"(?<![-\w.]){re.escape(bundle)}", text) is not None


def test_skill_md_advertises_no_bundle_this_phase_does_not_publish():
    """SKILL.md used to list all four bundles as things an agent could fetch. Three of them are
    made of appendices phase 2 does not render, so `site._write_bundles` never writes them and
    the fetch 404s -- and the agent has been told by its own skill that the file exists."""
    have = {p.name for p in (INDEX.parent).glob("*.md")} | {"00-index.md", "31-today-forums.md"}
    available = {b for b, parts in BUNDLES.items() if any(p in have for p in parts)}
    assert available == {"today.md"}, "the phase's published bundles moved; check SKILL.md"
    _, text = frontmatter()
    for bundle in set(BUNDLES) - available:
        assert not _names(bundle, text), f"SKILL.md advertises {bundle}, which is not published"
    # The one that does exist stays as the worked example, and the index is named as the
    # authority on the rest, so the skill needs no edit when a later phase adds one.
    assert _names("today.md", text)
    assert "index footer" in text


def test_manifests_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == "aisafety-radar"
    assert market["plugins"][0]["name"] == "aisafety-radar"
    assert plugin["version"] == market["plugins"][0]["version"]
