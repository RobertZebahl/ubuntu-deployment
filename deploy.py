"""
Deployment entry point.

Reads data/entries.yml and dispatches each entry to the matching PyInfra
operation based on its `type` field. See data/entries.yml for the
schema of each type.

PyInfra evaluates every fact while this file is being loaded, before any
operation has run. Facts therefore cannot depend on something an earlier
entry installs; tools the facts need (curl, git) are installed by
bootstrap.py, which deploy.sh runs first.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable

from typing_extensions import override

from pyinfra import config, host, logger
from pyinfra.api import FactBase
from pyinfra.facts.deb import DebPackages
from pyinfra.facts.files import File
from pyinfra.facts.server import Home, KernelVersion, User
from pyinfra.facts.server import Path as PathFact
from pyinfra.operations import apt, files, git, server, snap, uv

from aptlock import wait_for_apt_lock
from entries import DATA_FILE, is_bootstrap, load_entries, needs_of
from release import check_release


class GpuVendor(FactBase[str]):
    """
    Detected GPU vendor from `lspci`: "amd", "nvidia", "intel", or "unknown".
    Used to gate entries with `when_gpu:` in data/entries.yml.
    """

    @override
    @staticmethod
    def default() -> str:
        return "unknown"

    @override
    def requires_command(self, *args, **kwargs) -> str:
        return "lspci"

    @override
    def command(self) -> str:
        return "lspci -mm | grep -Ei 'VGA compatible controller|3D controller' || true"

    @override
    def process(self, output: Iterable[str]) -> str:
        text = " ".join(output).lower()
        if "amd" in text or "advanced micro devices" in text:
            return "amd"
        if "nvidia" in text:
            return "nvidia"
        if "intel" in text:
            return "intel"
        return "unknown"


class GithubLatestDebAsset(FactBase[dict | None]):
    """
    Returns {"url": ..., "version": ...} for a .deb asset attached to a
    GitHub repo's latest release: the asset's download URL and the release's
    version (its tag without a leading "v"). For software (like Heroic) that
    ships a .deb directly on GitHub Releases rather than through an apt
    repository.

    Returns None if `curl` isn't on the target (PyInfra then falls back to
    default() without running anything), and {} if the release has no
    matching .deb - the caller treats those two differently.

    + repo: "owner/name" on GitHub
    + match: substring the asset filename must contain, to disambiguate
      when a release has more than one .deb (e.g. an arch tag)
    """

    @override
    @staticmethod
    def default() -> dict | None:
        return None

    @override
    def requires_command(self, *args, **kwargs) -> str:
        return "curl"

    @override
    def command(self, repo: str, match: str = "") -> str:
        self._match = match
        return f"curl -fsSL https://api.github.com/repos/{repo}/releases/latest"

    @override
    def process(self, output: Iterable[str]) -> dict:
        try:
            data = json.loads("\n".join(output))
        except json.JSONDecodeError:
            return {}
        for asset in data.get("assets", []):
            asset_name = asset.get("name", "")
            if asset_name.endswith(".deb") and self._match in asset_name:
                return {
                    "url": asset.get("browser_download_url", ""),
                    "version": data.get("tag_name", "").removeprefix("v"),
                }
        return {}


class ShellCondition(FactBase[bool]):
    """
    Runs an arbitrary shell command on the target and reports whether it
    exited successfully. Backs the `unless:` guard on `shell` entries.
    """

    @override
    @staticmethod
    def default() -> bool:
        return False

    @override
    def command(self, test_command: str) -> str:
        return f"({test_command}) && echo __TRUE__ || echo __FALSE__"

    @override
    def process(self, output: Iterable[str]) -> bool:
        return "__TRUE__" in " ".join(output)


class AptFetchedSources(FactBase[list]):
    """
    The URIs of the apt repositories whose package lists apt has actually
    downloaded (the ones `apt-cache policy` prints), each without a trailing
    slash. A repository that was configured but never fetched, say because
    the run died between adding it and `apt update`, is missing here.
    """

    @override
    @staticmethod
    def default() -> list:
        return []

    @override
    def command(self) -> str:
        return "LC_ALL=C apt-cache policy"

    @override
    def process(self, output: Iterable[str]) -> list:
        uris = []
        for line in output:
            parts = line.split()
            # "<priority> <uri> <suite/component> <arch> Packages" (a flat repository has no suite)
            if len(parts) >= 2 and parts[0].isdigit() and "://" in parts[1]:
                uris.append(parts[1].rstrip("/"))
        return uris


# A keyring counts as expired when it has primary keys and every one of them is
# expired: `gpg --with-colons` prints a `pub` line per primary key, whose second
# field is "e" for an expired one. Reads the keyring on stdin; exits 0 if expired.
# (A signing *subkey* that expired on its own isn't looked at.)
KEYRING_EXPIRED_AWK = (
    "awk -F: '$1==\"pub\"{n++} $1==\"pub\" && $2!=\"e\"{ok=1} END{exit !(n>0 && !ok)}'"
)


def keyring_expired_command(path: str) -> str:
    """
    Shell test for the ShellCondition fact: is the keyring file at `path`
    expired? gpg gets a throwaway home directory so that a fact doesn't create
    ~/.gnupg on the target. Without gpg (or without the file) the answer is no.
    """
    quoted = shlex.quote(path)
    return (
        "d=$(mktemp -d); trap 'rm -rf \"$d\"' EXIT; "
        f'test -r {quoted} && GNUPGHOME="$d" gpg --batch --quiet --show-keys --with-colons {quoted} '
        f"2>/dev/null | {KEYRING_EXPIRED_AWK}"
    )


def fetch_key_command(src: str, dest: str, entry_name: str) -> str:
    """
    Download a repository's signing key, refuse it if it has expired, and
    install it (dearmored, mode 644) at `dest`. Used instead of `apt.key`
    whenever `dest` is missing or holds an expired key: `apt.key` leaves an
    existing file alone, so it would never pick up a renewed key, and it
    would install a key that upstream let expire without saying so.
    """
    message = (
        f"The signing key at {src} has expired, and upstream has not published "
        f"a renewed one yet. Try again later, or remove the entry "
        f"'{entry_name}' from data/entries.yml."
    )
    return "\n".join(
        [
            "set -e",
            'tmp=$(mktemp); gnupg=$(mktemp -d); trap \'rm -rf "$tmp" "$gnupg"\' EXIT',
            f'curl -fsSL {shlex.quote(src)} -o "$tmp"',
            'export GNUPGHOME="$gnupg"',
            f'if gpg --batch --quiet --show-keys --with-colons "$tmp" | {KEYRING_EXPIRED_AWK}; then',
            f"  echo {shlex.quote(message)} >&2; exit 1",
            "fi",
            f'mkdir -p "$(dirname {shlex.quote(dest)})"',
            f'gpg --batch --yes --dearmor -o {shlex.quote(dest)} "$tmp"',
            f"chmod 644 {shlex.quote(dest)}",
        ]
    )


def entry_applies(entry: dict) -> bool:
    wanted_gpu = entry.get("when_gpu")
    return wanted_gpu is None or host.get_fact(GpuVendor) == wanted_gpu


def expand_home(path: str) -> str:
    """
    Resolve a leading `~` to the target's actual home directory.

    Several PyInfra operations (`git.repo`, `files.put`) build their shell
    commands by quoting `dest` as a single opaque string, so an unresolved
    `~` is never expanded by the shell - it ends up creating a literal
    directory/file named `~` next to wherever the command runs, instead of
    going into the home directory. Facts like `files.File` special-case
    this themselves, but these two operations don't, so entries route
    their `dest` through here first.
    """
    home = host.get_fact(Home)
    if path == "~":
        return home
    if path.startswith("~/"):
        return f"{home}{path[1:]}"
    return path


def uv_path_env() -> dict:
    """
    uv lives in ~/.local/bin, where its installer puts it (deploy.sh may have
    just installed it). That directory might not be on the PATH the shell has,
    so prepend it explicitly for uv-tool/uv-pip commands rather than relying
    on a shell profile reload.
    """
    return {"PATH": f"{host.get_fact(Home)}/.local/bin:{host.get_fact(PathFact)}"}


def resolve_package_names(packages: list[str]) -> list[str]:
    """
    Substitute `{kernel_release}` in apt package names with the target's
    actual running kernel version (e.g. "linux-headers-{kernel_release}" ->
    "linux-headers-6.14.0-37-generic"). This can't be hardcoded in the YAML
    since it varies per host and changes on kernel upgrades.
    """
    if not any("{kernel_release}" in p for p in packages):
        return packages
    kernel_release = host.get_fact(KernelVersion)
    return [p.format(kernel_release=kernel_release) for p in packages]


# did_change callbacks of the repository operations (apt.key, apt.sources_file,
# apt.ppa) run since the last `apt update`; see refresh_apt_if_repos_changed().
# A repository that apt has never fetched adds an always-true entry, see
# note_unfetched_repo().
pending_repo_changes: list = []


def note_unfetched_repo(fetched: bool) -> None:
    """
    Make the next `update: true` entry run `apt update` if a repository isn't
    in apt's package index yet, even though nothing about it changes in this
    run. That is what a rerun after a failure between adding the repository
    and its `apt update` looks like: the sources file and key are in place,
    so their operations report "No changes", and without this the update
    would be skipped for good and a package could be installed from the
    wrong source (Ubuntu's own, say).
    """
    if not fetched:
        pending_repo_changes.append(lambda: True)


def refresh_apt_if_repos_changed(name: str) -> None:
    """
    Run `apt update` before an `apt` entry marked `update: true`, but only if
    a repository entry changed something in this same run - a repo that was
    just added is not in apt's index yet, an unchanged one is. Otherwise every
    run would re-download the package lists just to change nothing.
    """
    if not pending_repo_changes:
        return
    changes = list(pending_repo_changes)
    pending_repo_changes.clear()
    apt.update(
        name=f"{name} (refresh package index, apt sources changed or not fetched yet)",
        _if=[lambda: any(changed() for changed in changes)],
        _sudo=True,
    )


def ppa_guard(ppa: str) -> list:
    """
    apt-add-repository always re-runs, so skip it if the PPA's sources file is
    already there (its URL contains ppa.launchpadcontent.net/<owner>/<name>/).
    """
    owner, _, ppa_name = ppa.removeprefix("ppa:").partition("/")
    needle = shlex.quote(f"ppa.launchpadcontent.net/{owner}/{ppa_name}/")
    check = f"grep -rqsF {needle} /etc/apt/sources.list.d/"
    return [lambda: not host.get_fact(ShellCondition, check)]


def shell_guards(entry: dict) -> list:
    guards = []
    creates = entry.get("creates")
    if creates:
        guards.append(lambda p=creates: host.get_fact(File, path=p) is None)
    unless = entry.get("unless")
    if unless:
        guards.append(lambda c=unless: not host.get_fact(ShellCondition, c))
    return guards


# The operations each entry created, by entry name; the entries that don't
# apply on this machine (`when_gpu` doesn't match, the disk is absent, ...); and
# the entries skipped because one they need failed or doesn't apply. See
# needs_guard(). An entry skipped by `skip_if_exists` is in none of them: what
# it would provide is there already, so what needs it is fine.
entry_ops: dict[str, list] = {}
not_applied: set[str] = set()
blocked_entries: set[str] = set()


def track(name: str, op):
    entry_ops.setdefault(name, []).append(op)
    return op


def needs_guard(entry: dict, name: str) -> list:
    """
    `_if` guard for every operation of an entry with `needs:`. The run goes on
    after an error (see below), so an entry must not be applied when one it
    needs failed: a package would be installed from the wrong source (Ubuntu's
    own darktable, say) if the repository entry before it failed. Nor when one
    it needs doesn't apply on this machine, since what it builds on isn't
    there. Such an entry is skipped with a warning, and so are the entries
    that need it.
    """
    needs = needs_of(entry)
    if not needs:
        return []

    def problem() -> tuple[str, str] | None:
        for n in needs:
            if n in not_applied:
                return n, "doesn't apply on this machine"
            if n in blocked_entries:
                return n, "was skipped"
            if any(op.did_error() for op in entry_ops.get(n, [])):
                return n, "failed"
        return None

    def satisfied() -> bool:
        found = problem()
        if found is None:
            return True
        if name not in blocked_entries:
            blocked_entries.add(name)
            logger.warning(f"{host.name}: skipping {name!r}: it needs {found[0]!r}, which {found[1]}")
        return False

    return [satisfied]


check_release()
entries = load_entries()

# Once per run, not before every apt operation: the trouble is background
# apt jobs right after a boot or login, and a wait step per operation would
# print a "No changes" line each time.
wait_for_apt_lock()

# From here on, an entry that fails doesn't stop the run: the others are still
# applied, and the failure is listed as an error at the end. Set after the apt
# lock wait, so that a machine that stays locked still stops the run.
config.IGNORE_ERRORS = True

for entry in entries:
    if is_bootstrap(entry):
        continue  # bootstrap entries were applied by bootstrap.py
    if not entry_applies(entry):
        not_applied.add(entry["name"])
        continue

    entry_type = entry["type"]
    name = entry["name"]
    needs_ok = needs_guard(entry, name)

    # Checked while loading, so it only sees what was there before this run.
    skip_if_exists = entry.get("skip_if_exists")
    if skip_if_exists and host.get_fact(File, path=expand_home(skip_if_exists)) is not None:
        continue

    if entry_type == "apt":
        if entry.get("update"):
            refresh_apt_if_repos_changed(name)
        track(name, apt.packages(
            name=name,
            packages=resolve_package_names(entry["packages"]),
            _if=needs_ok,
            _sudo=True,
        ))

    elif entry_type == "apt-repo":
        key_dest = entry.get("key_dest")
        signed_by = None
        key = None
        if key_dest:
            signed_by = key_dest if key_dest.startswith("/") else f"/etc/apt/keyrings/{key_dest}"

        if entry.get("key_src"):
            # A key that is missing or has expired is fetched (and checked) by
            # our own command; a present one is left to apt.key, which only
            # keeps its mode right.
            if signed_by and (
                host.get_fact(File, path=signed_by) is None
                or host.get_fact(ShellCondition, keyring_expired_command(signed_by))
            ):
                key = track(name, server.shell(
                    name=f"{name} (GPG key)",
                    commands=[fetch_key_command(entry["key_src"], signed_by, name)],
                    _if=needs_ok,
                    _sudo=True,
                ))
            else:
                key = track(name, apt.key(
                    name=f"{name} (GPG key)",
                    src=entry["key_src"],
                    dest=key_dest,
                    _if=needs_ok,
                    _sudo=True,
                ))
            pending_repo_changes.append(key.did_change)

        uris = entry["uris"].split() if isinstance(entry["uris"], str) else entry["uris"]
        if "deb" in str(entry.get("types", "deb")).split():
            fetched = host.get_fact(AptFetchedSources)
            note_unfetched_repo(all(uri.rstrip("/") in fetched for uri in uris))

        # No sources file without its key: apt would fail on it, at every later
        # `apt update` too, until somebody removes it.
        sources = track(name, apt.sources_file(
            name=name,
            filename=entry["filename"],
            types=entry.get("types", "deb"),
            uris=entry["uris"],
            suites=entry["suites"],
            components=entry.get("components"),
            architectures=entry.get("architectures"),
            signed_by=signed_by,
            _if=needs_ok + ([key.did_succeed] if key else []),
            _sudo=True,
        ))
        pending_repo_changes.append(sources.did_change)

    elif entry_type == "apt-ppa":
        # apt-add-repository (from software-properties-common, which the
        # file installs in its own entry) understands ppa:owner/name.
        owner, _, ppa_name = entry["ppa"].removeprefix("ppa:").partition("/")
        ppa_uri = f"ppa.launchpadcontent.net/{owner}/{ppa_name}/"
        note_unfetched_repo(any(ppa_uri in uri + "/" for uri in host.get_fact(AptFetchedSources)))
        ppa = track(name, apt.ppa(
            name=name, src=entry["ppa"], _if=needs_ok + ppa_guard(entry["ppa"]), _sudo=True
        ))
        pending_repo_changes.append(ppa.did_change)

    elif entry_type == "github-deb":
        asset = host.get_fact(
            GithubLatestDebAsset, repo=entry["repo"], match=entry.get("match", "")
        )
        if asset is None:
            # Only happens before bootstrap.py has installed curl, i.e. a dry
            # run on a bare machine; a real run always has curl by now.
            not_applied.add(name)
            logger.warning(
                f"{host.name}: skipping {name!r}: curl is not installed yet, so the "
                f"latest release can't be looked up (run bootstrap.py first)"
            )
            continue
        if not asset:
            raise ValueError(
                f"Could not find a matching .deb asset on the latest release "
                f"of {entry['repo']!r} for entry {name!r}"
            )
        # apt.deb can't tell that a URL's .deb is already installed (the file
        # isn't downloaded yet while the run is planned), so with `package:`
        # given, compare the release version with the installed one ourselves.
        package = entry.get("package")
        guards = list(needs_ok)
        if package:
            guards.append(
                lambda p=package, v=asset["version"]: v not in host.get_fact(DebPackages).get(p, [])
            )
        track(name, apt.deb(name=name, src=asset["url"], _if=guards, _sudo=True))

    elif entry_type == "snap":
        track(name, snap.package(
            name=name,
            packages=entry["packages"],
            channel=entry.get("channel", "latest/stable"),
            classic=entry.get("classic", False),
            _if=needs_ok,
            _sudo=True,
        ))

    elif entry_type == "user-group":
        track(name, server.user(
            name=name,
            user=entry.get("user") or host.get_fact(User),
            groups=entry["groups"],
            append=True,
            _if=needs_ok,
            _sudo=True,
        ))

    elif entry_type == "git":
        track(name, git.repo(
            name=name,
            src=entry["src"],
            dest=expand_home(entry["dest"]),
            branch=entry.get("branch"),
            _if=needs_ok,
            _sudo=entry.get("sudo", False),
        ))

    elif entry_type == "uv-tool":
        track(name, uv.tools(name=name, tools=entry["tools"], _if=needs_ok, _env=uv_path_env()))

    elif entry_type == "uv-pip":
        track(name, uv.packages(
            name=name, packages=entry["packages"], _if=needs_ok, _env=uv_path_env()
        ))

    elif entry_type == "shell":
        track(name, server.shell(
            name=name,
            commands=entry["commands"],
            _sudo=entry.get("sudo", False),
            _if=needs_ok + shell_guards(entry),
        ))

    elif entry_type == "fstab":
        device = entry["device"]
        # Only for disks that are actually present, so the same YAML can be
        # used on machines that don't have this disk.
        if not host.get_fact(ShellCondition, f"test -e {shlex.quote(device)}"):
            not_applied.add(name)
            continue

        mountpoint = expand_home(entry["mountpoint"])
        fstab_line = " ".join(
            [
                device,
                mountpoint,
                entry.get("fstype", "auto"),
                entry.get("options", "defaults"),
                str(entry.get("dump", 0)),
                str(entry.get("pass", 0)),
            ]
        )

        track(name, files.directory(
            name=f"{name} (mount point)",
            path=mountpoint,
            _if=needs_ok,
            _sudo=entry.get("sudo", False),
        ))
        # Match by device so a differing existing line is replaced rather
        # than duplicated; an identical one is left alone.
        track(name, files.line(
            name=f"{name} (/etc/fstab)",
            path="/etc/fstab",
            line=f"^{device}[[:space:]]",
            replace=fstab_line,
            _if=needs_ok,
            _sudo=True,
        ))
        # fstab changes need a daemon-reload before systemd sees them. A failed
        # mount must not abort the rest of the deployment run (the fstab
        # entry is `nofail`-style anyway).
        server.shell(
            name=f"{name} (mount now)",
            commands=[f"systemctl daemon-reload && mount {shlex.quote(mountpoint)}"],
            _sudo=True,
            _ignore_errors=True,
            _if=needs_ok
            + [lambda mp=mountpoint: not host.get_fact(ShellCondition, f"mountpoint -q {shlex.quote(mp)}")],
        )

    elif entry_type == "file":
        track(name, files.put(
            name=name,
            src=entry["src"],
            dest=expand_home(entry["dest"]),
            mode=entry.get("mode"),
            _if=needs_ok,
            _sudo=entry.get("sudo", False),
        ))

    else:
        raise ValueError(f"Unknown type {entry_type!r} for entry {name!r} in {DATA_FILE}")
