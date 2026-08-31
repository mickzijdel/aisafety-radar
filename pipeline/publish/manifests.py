"""Set the plugin version in every manifest that carries one.

A marketplace file repeats the version inside `plugins[*]`, and a marketplace entry whose version
disagrees with the plugin's own manifest is what makes an installed copy refuse to update. Both
are written from the one `plugin_version(pack_version)` string.

`plugin_version` is called once, before anything is written, so a pack version that is not a pack
version fails with nothing half-updated.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.versioning import plugin_version


def bump(pack_version: str, files: list[Path]) -> str:
    """Set `version` (and each `plugins[*].version`) in `files`; return the version written."""
    version = plugin_version(pack_version)
    for path in files:
        data = json.loads(path.read_text())
        data["version"] = version
        for plugin in data.get("plugins", []):
            plugin["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")
    return version
