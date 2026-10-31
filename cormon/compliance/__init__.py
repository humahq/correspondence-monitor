"""Compliance components (21 CFR Part 11), each a thin wrapper with one job and a backend chosen in settings:

    records  originals and sent messages, each with its SHA-256       compliance.records: local
    trail    the audit log, hash-chained, with a daily anchor         compliance.trail: local
    vault    write-once copies of originals, sent messages, audit     compliance.vault: separate_db
    ailog    every AI call in full (prompt, answer, versions)         compliance.ailog: local
    secrets  every credential, from one place                         compliance.secrets: local
    alerts   telling a person the bot is unhealthy                    compliance.alerts: email

`local` keeps everything on this machine at no cost. The certified backends (a write-once Cloud Storage bucket,
Secret Manager only, a pager) plug in behind the same functions; until one is built, naming it fails loudly at
the first use rather than quietly falling back to local.
"""

from __future__ import annotations

import hashlib

from .. import settings

# SAF-11: shown on every digest and page, word for word
SAFETY_NOTICE = "This system does not guarantee the detection of safety signals. Silence does not constitute clearance."

DEFAULTS = {"records": "local", "trail": "local", "vault": "separate_db", "ailog": "local", "secrets": "local",
            "alerts": "email"}


def backend(component: str, built: tuple[str, ...]) -> str:
    name = str(settings.get(f"compliance.{component}") or DEFAULTS[component])
    if name not in built:
        raise NotImplementedError(f"compliance.{component}: backend '{name}' is not built yet (have: {', '.join(built)})")
    return name


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()
