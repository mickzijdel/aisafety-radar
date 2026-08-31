"""The shipped skill package: frontmatter, body limits, manifests and the fetch script.

Everything here reads the files a user installs, not the pipeline that regenerates them, so
the assertions are deliberately about bytes on disk: an agent runtime parses SKILL.md's
frontmatter itself and will reject keys the spec does not define.
"""

import json
import re
from pathlib import Path

import yaml

from pipeline.render import PAGES_BASE

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "aisafety-radar" / "SKILL.md"
SCRIPT = ROOT / "skills" / "aisafety-radar" / "scripts" / "fetch-latest.sh"
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
    # fetch, and the URL the script actually fetches all name the same host and path. Three
    # copies of one string, so the test reads the Python constant rather than repeating it.
    fm, _ = frontmatter()
    assert fm["metadata"]["latest"].startswith(PAGES_BASE)
    base_line = next(line for line in SCRIPT.read_text().splitlines() if line.startswith("base="))
    assert base_line.split("=", 1)[1].strip('"').startswith(PAGES_BASE)


def test_fetch_script_verifies_a_checksum_before_printing():
    script = SCRIPT.read_text()
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert "checksums.txt" in script
    assert "sha256sum" in script
    # A mismatch, or a file with no published checksum, must exit non-zero without printing
    # the body: the caller falls back to the bundled copy rather than trusting the download.
    assert script.count("exit 1") == 2
    assert script.index("sha256sum") < script.index("cat ")


def test_manifests_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == "aisafety-radar"
    assert market["plugins"][0]["name"] == "aisafety-radar"
    assert plugin["version"] == market["plugins"][0]["version"]
