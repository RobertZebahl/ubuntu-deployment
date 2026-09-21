# PyInfra inventory: the machine this tool deploys to, which is only this one
# (`@local`, no SSH). deploy.sh supplies the sudo password through
# DEPLOY_SUDO_PASSWORD so that its two PyInfra runs (bootstrap.py, deploy.py)
# only ask for it once.
import os

# Read the password, then remove it from the environment: every command a
# deployment runs (installer scripts, npx, ...) is a child process of PyInfra
# and would otherwise inherit it. It stays in the host data below, which
# PyInfra only uses for `sudo`.
_sudo_password = os.environ.pop("DEPLOY_SUDO_PASSWORD", None)

hosts = [
    ("@local", {"_sudo_password": _sudo_password} if _sudo_password else {}),
]
