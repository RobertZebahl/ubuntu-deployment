#!/usr/bin/env bash
# Convenience wrapper: run this to deploy to the local machine.
#   ./deploy.sh            apply (bootstrap, then deploy)
#   ./deploy.sh --dry      preview without applying
#   ./deploy.sh -y         apply without the confirmation prompts
# Arguments are forwarded to pyinfra.
#
# A deployment is two PyInfra runs, because PyInfra evaluates all facts
# before running any operation: bootstrap.py installs what deploy.py's facts
# need (curl, git), then deploy.py applies data/entries.yml.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# uv runs everything below. On a fresh machine it isn't installed yet, so get
# it with its official installer, which puts it in ~/.local/bin (a directory a
# fresh login shell may not have on its PATH yet).
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo ">> uv is not installed; installing it (https://astral.sh/uv/install.sh) ..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "Error: uv is not installed, and neither curl nor wget is available to install it." >&2
    echo "Run 'sudo apt install curl' and start this again, or install uv yourself: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  command -v uv >/dev/null 2>&1 || { echo "Error: uv is still not on the PATH after installing it." >&2; exit 1; }
fi

# Stop on a release this isn't written for before asking for anything, or
# changing anything (release.py needs no packages, so no project environment).
uv run --no-project python release.py

# @local needs a sudo password; ask once for both runs.
if [[ -z ${DEPLOY_SUDO_PASSWORD:-} ]]; then
  read -rsp "[sudo] password for $USER: " DEPLOY_SUDO_PASSWORD
  echo
  export DEPLOY_SUDO_PASSWORD
fi

uv run pyinfra "$@" inventory.py bootstrap.py
uv run pyinfra "$@" inventory.py deploy.py
