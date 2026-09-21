"""
The Ubuntu release this deployment is written for, and the check for it.

Several repositories in data/entries.yml are pinned to this release (its
codename is "resolute"): change them together with SUPPORTED_VERSION_ID.

Kept free of PyInfra imports, like entries.py, so that deploy.sh can run it
(`python release.py`) before anything else, and bootstrap.py and deploy.py can
call check_release() when PyInfra is started directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

SUPPORTED_VERSION_ID = "26.04"
SUPPORTED_NAME = "Ubuntu 26.04 LTS (resolute)"

OS_RELEASE = Path("/etc/os-release")


def read_os_release(path: Path = OS_RELEASE) -> dict[str, str]:
    """The KEY=value pairs of an os-release file; {} if it can't be read."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    pairs = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            pairs[key.strip()] = value.strip().strip("\"'")
    return pairs


def check_release(path: Path = OS_RELEASE) -> None:
    """Raise if this machine doesn't run the release the deployment is written for."""
    info = read_os_release(path)
    if info.get("ID") == "ubuntu" and info.get("VERSION_ID") == SUPPORTED_VERSION_ID:
        return
    found = info.get("PRETTY_NAME") or "an unknown system"
    raise RuntimeError(
        f"This deployment is written for {SUPPORTED_NAME}, but this machine runs {found}. "
        f"Several third-party repositories in data/entries.yml are pinned to that release, "
        f"so nothing was changed."
    )


if __name__ == "__main__":
    try:
        check_release()
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
