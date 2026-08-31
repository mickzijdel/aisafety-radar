"""The published site: `latest/`, the per-version archive, `checksums.txt` and `llms.txt`.

Three contracts leave this module and are relied on elsewhere:

* **`checksums.txt` is `sha256sum` text format** -- `<64 hex><two spaces><bare file name>`, one
  file per line. `skills/aisafety-radar/scripts/fetch-latest.sh` reads it with `awk '$2 == n'`
  and refuses to print a file it cannot find a checksum for, so a path in column 2 or a single
  space turns every freshness fetch into a hard failure. The digests are taken from the copies in
  `latest/` rather than from the pack, so what is hashed is what is served.
* **A bundle is a recipe in one fetch.** The file lists come from `pipeline.render.RECIPES`, the
  same lists the index advertises, so the two cannot drift; `map.md` is the one bundle without a
  recipe, because "a close look at one area" pairs two files a reader picks per area while the
  map layer as a whole is worth one fetch (SKILL.md names all four).
* **`latest/` is rebuilt, not updated.** An appendix that stops being generated has to disappear
  from the site rather than sit there with a stale date under a fresh `checksums.txt`.

A bundle is written only when at least one of its parts exists: an empty `week.md` published
under a checksum would tell a reader "this week had nothing in it", which is a claim, not a gap.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from pipeline.render import APPENDIX_PURPOSE, RECIPES

# Bundle file name per recipe name in `RECIPES`. The recipes are the source of truth for the
# file lists; this only names the download.
_RECIPE_BUNDLES = {
    "what happened today": "today.md",
    "this week": "week.md",
    "career change": "careers.md",
}

# The slow layer in one fetch. No recipe matches it: "a close look at one area" is two files
# chosen per area, not the whole map.
_MAP_BUNDLE = [
    "10-map-field-overview.md",
    "11-map-orgs.md",
    "12-map-programmes.md",
    "13-map-glossary.md",
    "14-map-readings.md",
]

_recipe_files = dict(RECIPES)
assert _RECIPE_BUNDLES.keys() <= _recipe_files.keys(), "a bundle names a recipe that is gone"

BUNDLES: dict[str, list[str]] = {
    bundle: _recipe_files[recipe] for recipe, bundle in _RECIPE_BUNDLES.items()
} | {"map.md": _MAP_BUNDLE}

CHECKSUMS = "checksums.txt"


def _replace_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _write_bundles(pack_dir: Path, latest: Path, names: set[str]) -> dict[str, list[str]]:
    """Write each bundle whose parts this pack actually has; return what went into each."""
    written: dict[str, list[str]] = {}
    for bundle, parts in BUNDLES.items():
        present = [p for p in parts if p in names]
        if not present:
            continue
        (latest / bundle).write_text("".join((pack_dir / p).read_text() for p in present))
        written[bundle] = present
    return written


def _write_checksums(latest: Path) -> None:
    """`sha256sum` text format over every markdown file served from `latest/`."""
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(latest.glob("*.md"))
    ]
    (latest / CHECKSUMS).write_text("\n".join(lines) + "\n")


def _write_llms_txt(
    site_dir: Path, latest: Path, version: str, bundles: dict[str, list[str]]
) -> None:
    """The machine-readable index at the site root: every served file, with its purpose."""
    lines = [
        "# aisafety-radar",
        "",
        f"> Daily-updated AI safety landscape for AI agents. Version {version}.",
        "",
        "## Files",
        "",
    ]
    for path in sorted(latest.glob("*.md")):
        purpose = APPENDIX_PURPOSE.get(path.name) or (
            f"bundle: {', '.join(bundles[path.name])}" if path.name in bundles else ""
        )
        lines.append(f"- [{path.name}](latest/{path.name}): {purpose}")
    lines += [
        f"- [{CHECKSUMS}](latest/{CHECKSUMS}): sha256 of every file above, `sha256sum` format",
        "",
        f"Past versions are kept under `versions/<version>/`; this one is `versions/{version}/`.",
    ]
    (site_dir / "llms.txt").write_text("\n".join(lines) + "\n")


def write_latest(pack_dir: Path, site_dir: Path, version: str) -> None:
    """Publish `pack_dir` as `site_dir/latest/`, `site_dir/versions/<version>/` and `llms.txt`."""
    latest = site_dir / "latest"
    _replace_dir(latest)
    files = sorted(pack_dir.glob("*.md"))
    for path in files:
        shutil.copy(path, latest / path.name)
    bundles = _write_bundles(pack_dir, latest, {p.name for p in files})
    _write_checksums(latest)
    versioned = site_dir / "versions" / version
    if versioned.exists():
        shutil.rmtree(versioned)
    versioned.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(latest, versioned)
    _write_llms_txt(site_dir, latest, version, bundles)
