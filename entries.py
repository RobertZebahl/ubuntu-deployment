"""
Loading and validating the entries of data/entries.yml.

Kept free of PyInfra imports so that bootstrap.py and deploy.py (the two
steps of a deployment) read entries the same way.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DATA_FILE = Path(__file__).resolve().parent / "data" / "entries.yml"


def is_bootstrap(entry: dict) -> bool:
    """
    Entries marked `bootstrap: true` install what deploy.py's facts need. They
    are applied by bootstrap.py, not by deploy.py.
    """
    return entry.get("bootstrap") is True


def needs_of(entry: dict) -> list[str]:
    """The entry's `needs:` as a list (the YAML allows one name or a list)."""
    needs = entry.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def load_entries() -> list[dict]:
    with DATA_FILE.open() as f:
        entries = yaml.safe_load(f) or []
    validate_entries(entries)
    return entries


def validate_entries(entries: list[dict]) -> None:
    seen_names: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        # The name is the entry's identity: `needs:` refers to it, and so do
        # the results deploy.py keeps per entry.
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Entry number {position} ({entry.get('type')!r}) in {DATA_FILE} has no "
                f"`name`; every entry needs one, and it must be unique"
            )
        if name in seen_names:
            raise ValueError(
                f"Entry name {name!r} is used more than once in {DATA_FILE} "
                f"(entry number {position}); names must be unique"
            )

        if "mode" in entry:
            mode = entry["mode"]
            # Unquoted 0644 would be read by YAML as the decimal number 420.
            if entry["type"] != "file" or not (
                isinstance(mode, str) and mode.isdigit() and set(mode) <= set("01234567")
            ):
                raise ValueError(
                    f"`mode` in entry {name!r} in {DATA_FILE} must be a quoted octal string "
                    f"like \"644\" and is only for `file` entries"
                )

        if "bootstrap" in entry:
            if not isinstance(entry["bootstrap"], bool):
                raise ValueError(f"`bootstrap` must be true or false in entry {name!r} in {DATA_FILE}")
            if entry["bootstrap"]:
                if entry["type"] != "apt":
                    raise ValueError(
                        f"Entry {name!r} in {DATA_FILE}: only `apt` entries can be `bootstrap: true`"
                    )
                unsupported = {"update", "needs", "when_gpu"} & set(entry)
                if unsupported:
                    raise ValueError(
                        f"Entry {name!r} in {DATA_FILE}: `bootstrap: true` entries can't use "
                        f"{sorted(unsupported)} (bootstrap.py runs before everything else)"
                    )

        for needed in needs_of(entry):
            if needed not in seen_names:
                raise ValueError(
                    f"Entry {name!r} needs {needed!r}, which is not an earlier entry's name "
                    f"in {DATA_FILE} (entries are applied in file order)"
                )
        seen_names.add(name)


def bootstrap_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if is_bootstrap(e)]
