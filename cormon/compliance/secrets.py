"""Every credential the bot reads, from one place. First found wins: the environment variable named, then Google
Secret Manager (on a Google Cloud VM with the machine's own account, elsewhere through gcloud).

Certified backend (later): Secret Manager only, per-person access and rotation; the environment is not read.
"""

from __future__ import annotations

import base64
import os
import subprocess

from . import backend

BUILT = ("local",)


def get(*, env: str | None = None, secret: dict | None = None, what: str = "a secret") -> str | None:
    """The value, or None when neither source is configured. A configured source that fails raises."""
    backend("secrets", BUILT)
    if env and os.environ.get(env):
        return os.environ[env]
    if not (secret or {}).get("name"):
        return None
    value = _from_vm(secret, what)
    if value is not None:
        return value
    cmd = ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret['name']}"]
    if secret.get("project"):
        cmd.append(f"--project={secret['project']}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError(f"gcloud is not installed, and {what} is not in the environment") from None
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise RuntimeError(f"could not read {what} from Secret Manager: {out.stderr.strip()[:300]}")


def _from_vm(secret: dict, what: str) -> str | None:
    """On a Google Cloud VM, read the secret with the VM's own service account (no gcloud, no copied secret).
    None when not on Google Cloud."""
    import requests
    meta = "http://metadata.google.internal/computeMetadata/v1"
    try:
        tok = requests.get(f"{meta}/instance/service-accounts/default/token",
                           headers={"Metadata-Flavor": "Google"}, timeout=2).json()["access_token"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    project = secret.get("project") or requests.get(f"{meta}/project/project-id",
                                                    headers={"Metadata-Flavor": "Google"}, timeout=2).text
    r = requests.get(f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{secret['name']}"
                     "/versions/latest:access", headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"could not read {what} from Secret Manager: {r.text[:300]}")
    return base64.b64decode(r.json()["payload"]["data"]).decode().strip()
