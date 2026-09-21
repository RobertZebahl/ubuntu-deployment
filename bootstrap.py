"""
Phase one of the deployment: install what deploy.py's *facts* need.

PyInfra evaluates all facts while it loads deploy.py, before any of its
operations run, so a tool installed by an entry in entries.yml is not yet
there when a fact wants it (e.g. `curl` for the GitHub release lookup, `git`
for the git-clone state). deploy.sh runs this file first as its own
PyInfra invocation, so that everything it installs exists by the time
deploy.py is loaded.

What it installs is declared in data/entries.yml: the `apt` entries marked
`bootstrap: true`. deploy.py skips those entries. Keep them to exactly what
the facts need - everything else is a normal entry.
"""

from pyinfra.operations import apt

from aptlock import wait_for_apt_lock
from entries import bootstrap_entries, load_entries
from release import check_release

check_release()
wait_for_apt_lock()
# A third-party repository that is broken (an expired signing key, say) makes
# `apt update` fail, but everything installed here comes from Ubuntu's own
# archive, so that must not stop the deployment before deploy.py even starts.
# deploy.py stays strict: it fails on the broken repository, with the reason.
apt.update(
    name="Refresh the package index (a failing third-party repository is not fatal here)",
    cache_time=3600,  # skip `apt update` if the index is under an hour old
    _ignore_errors=True,
    _sudo=True,
)
for entry in bootstrap_entries(load_entries()):
    apt.packages(name=entry["name"], packages=entry["packages"], _sudo=True)
