"""
Wait for other apt/dpkg processes before a run's first apt operation.

On a freshly booted machine, unattended-upgrades and PackageKit run
`apt-get` in the background for a while, and apt fails immediately
("Could not get lock /var/lib/apt/lists/lock") instead of waiting for the
lists lock; `DPkg::Lock::Timeout` only covers the dpkg lock. So deploying
right after a boot fails at random unless it waits for them itself. It waits
once at the start of each run (bootstrap.py, deploy.py), not before every
operation: each wait is a step PyInfra prints, even when it does nothing.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable

from typing_extensions import override

from pyinfra import host
from pyinfra.api import FactBase
from pyinfra.operations import server

_LOCK_FILES = "/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock"

# `fuser` (psmisc) reports processes that have the lock files open. Without
# it there's nothing to wait on, so carry on rather than fail.
_WAIT_SCRIPT = f"""
command -v fuser >/dev/null || exit 0
for i in $(seq 150); do
  fuser {_LOCK_FILES} >/dev/null 2>&1 || exit 0
  sleep 2
done
echo "apt/dpkg is still locked by another process after 300 s" >&2
exit 1
"""

# PyInfra caches facts by their arguments, but whether apt is locked has to
# be looked at again every time; a counter argument makes each call unique.
_fresh = itertools.count()


class AptLocked(FactBase[bool]):
    """Whether another process currently has apt's or dpkg's lock files open."""

    @override
    @staticmethod
    def default() -> bool:
        return False

    @override
    def command(self, nonce: int = 0) -> str:  # `nonce` only defeats the fact cache
        return f"command -v fuser >/dev/null && fuser {_LOCK_FILES} >/dev/null 2>&1 && echo __LOCKED__ || echo __FREE__"

    @override
    def process(self, output: Iterable[str]) -> bool:
        return "__LOCKED__" in " ".join(output)


def wait_for_apt_lock() -> None:
    """
    Wait for other apt/dpkg processes to finish. Only does anything if one is
    running when this point of the run is reached, so on a quiet machine the
    step is skipped and a repeat run stays free of changes.
    """
    server.shell(
        name="Wait for other apt/dpkg processes to finish",
        commands=[_WAIT_SCRIPT],
        _sudo=True,
        _if=[lambda: host.get_fact(AptLocked, nonce=next(_fresh), _sudo=True)],
    )
