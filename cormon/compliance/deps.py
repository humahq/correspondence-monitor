"""The software the bot runs on: a list of every installed package and version (the bill of materials, SBOM),
checked against the public OSV database of known security problems. Run by `cormon scan` and once a week by the
listener; a known problem is recorded and the admins are told (SEC-02)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import metadata

from .. import db, settings

OSV = "https://api.osv.dev/v1/querybatch"


def packages() -> list[dict]:
    seen = {}
    for d in metadata.distributions():
        name = (d.metadata["Name"] or "").strip()
        if name:
            seen[name.lower()] = {"name": name, "version": d.version}
    return sorted(seen.values(), key=lambda p: p["name"].lower())


def scan(http=None) -> dict:
    """{"packages", "problems": [{name, version, ids}], "sbom": path}. Raises when OSV cannot be reached."""
    import requests
    http = http or requests
    pkgs = packages()
    problems = []
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        r = http.post(OSV, json={"queries": [{"package": {"name": p["name"], "ecosystem": "PyPI"},
                                              "version": p["version"]} for p in chunk]}, timeout=60)
        r.raise_for_status()
        for p, res in zip(chunk, r.json().get("results", [])):
            ids = [v["id"] for v in res.get("vulns") or []]
            if ids:
                problems.append(dict(p, ids=ids))
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = settings.data_dir() / "sbom.json"
    path.write_text(json.dumps({"made": when, "packages": pkgs, "known_problems": problems}, indent=1))
    db.audit("dependency_scan", entity_type="service", entity_id="deps", packages=len(pkgs),
             problems=[f"{p['name']} {p['version']}: {', '.join(p['ids'])}" for p in problems])
    return {"packages": len(pkgs), "problems": problems, "sbom": str(path)}


def weekly(log=print) -> dict | None:
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if db.get_state("deps_week") == week:
        return None
    db.set_state("deps_week", week)
    res = scan()
    from . import alerts
    if res["problems"]:
        lines = "\n".join(f"- {p['name']} {p['version']}: {', '.join(p['ids'][:5])}" for p in res["problems"])
        alerts.notify("deps", f"Study monitor: {len(res['problems'])} package(s) with known security problems",
                      f"The weekly check found known problems in software the bot uses:\n\n{lines}\n\n"
                      "Details for each id: https://osv.dev/vulnerability/<id>. Update the package, test, deploy.",
                      every_minutes=7 * 24 * 60)
    else:
        alerts.clear("deps", "Study monitor: no known security problems in the bot's software",
                     f"All {res['packages']} packages checked clean.")
    log(f"dependency scan: {res['packages']} packages, {len(res['problems'])} with known problems")
    return res
