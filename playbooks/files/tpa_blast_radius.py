#!/usr/bin/env python3
"""Compute TPA blast radius for Acme apps (stdin JSON → stdout JSON).

Input:
  {
    "tpa_url": "...",
    "token": "...",
    "apps": ["wire-transfer-svc", ...],
    "maven_group": "com.h2database",
    "maven_artifact": "h2",
    "gitlab_url": "https://gitlab...",
    "gitlab_group": "acme-cng2l",
    "verify": false
  }

Output:
  { "affected_repos": [...], "base_purl": "pkg:maven/...", "errors": [] }
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def get_json(url: str, token: str, ctx: ssl.SSLContext) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        return json.load(resp)


def pick_sbom(items: list, app: str) -> dict | None:
    named = [i for i in items if i.get("name") == app]
    if not named:
        return None
    cdx = [
        i
        for i in named
        if isinstance(i.get("labels"), dict) and i["labels"].get("type") == "cyclonedx"
    ]
    return (cdx or named)[0]


def package_hits(pkgs: list, base_purl: str) -> list[str]:
    hits: list[str] = []
    for pkg in pkgs:
        for p in pkg.get("purl") or []:
            purl = p.get("purl") if isinstance(p, dict) else None
            if purl and base_purl in purl:
                hits.append(purl)
    return hits


def main() -> int:
    cfg = json.load(sys.stdin)
    tpa = cfg["tpa_url"].rstrip("/")
    token = cfg["token"]
    apps = cfg.get("apps") or []
    group = cfg.get("maven_group") or ""
    artifact = cfg.get("maven_artifact") or ""
    gitlab_url = (cfg.get("gitlab_url") or "").rstrip("/")
    gitlab_group = cfg.get("gitlab_group") or ""
    verify = bool(cfg.get("verify", False))

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    errors: list[str] = []
    affected: list[dict] = []
    base_purl = f"pkg:maven/{group}/{artifact}" if group and artifact else ""

    if not base_purl:
        json.dump(
            {"affected_repos": [], "base_purl": "", "errors": ["missing maven coordinates"]},
            sys.stdout,
        )
        return 0

    seen: set[str] = set()
    for app in apps:
        try:
            listing = get_json(
                f"{tpa}/api/v2/sbom?q={urllib.parse.quote(app)}"
                f"&limit=20&sort={urllib.parse.quote('ingested:desc')}",
                token,
                ctx,
            )
        except Exception as exc:  # noqa: BLE001 — surface per-app errors
            errors.append(f"{app}: list sbom: {exc}")
            continue
        sbom = pick_sbom(listing.get("items") or [], app)
        if not sbom:
            continue
        sbom_id = sbom.get("id") or ""
        try:
            pkgs = get_json(
                f"{tpa}/api/v3/sbom/{urllib.parse.quote(sbom_id, safe='')}"
                f"/packages?q={urllib.parse.quote(artifact)}&limit=50",
                token,
                ctx,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{app}: packages: {exc}")
            continue
        hits = package_hits(pkgs.get("items") or [], base_purl)
        if not hits:
            continue
        path = f"{gitlab_group}/{app}" if gitlab_group else app
        if path in seen:
            continue
        seen.add(path)
        affected.append(
            {
                "repo_url": f"{gitlab_url}/{path}.git",
                "gitlab_path": path,
                "sbom_id": sbom_id,
                "sbom_name": app,
                "match_reason": "sbom_package_purl_contains_coordinate",
                "matched_purl": hits[0],
                "app_classification": "acme",
                "deployment_env": "development",
            }
        )

    json.dump(
        {"affected_repos": affected, "base_purl": base_purl, "errors": errors},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
