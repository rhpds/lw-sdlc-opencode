#!/usr/bin/env python3
"""Measure the two validation checks the agent cannot eyeball.

The agent fabricated `license` and `regression` because the skill described
what a good answer looks like and gave it no way to obtain one. Reasoning
about a licence string or "behavioural diff" from a green build is guessing;
these subcommands compute both from artefacts that actually exist, and print
a result object ready to paste into `gitlab_api.py mr-validation`.

  lw_checks.py license    --nexus-base URL --group G --artifact A \
                          --old-version V --new-version V [--repo R] [--proxy R]
  lw_checks.py regression --log JOB_LOG            # two-build verify Job
  lw_checks.py regression --base-dir D --head-dir D  # when reports are on disk

Both always print one JSON object on stdout and exit 0, even when the answer
is unwelcome. A `warning` that says what is unknown is a measurement; a
`passed` that was inferred is not.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET

POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# licence identity
#
# POMs spell the same licence many ways: "Apache-2.0", "Apache License,
# Version 2.0", "The Apache Software License, Version 2.0". Comparing the raw
# strings reports a licence change on every remediation, which trains people
# to ignore the row. Canonicalise to an SPDX-ish id first, and only report a
# change when the *identity* differs.
# --------------------------------------------------------------------------
_RULES: list[tuple[str, str]] = [
    (r"\bapache\b.*\b2(\.0)?\b|\bapache[-_ ]?2(\.0)?\b|\basl\b.*\b2(\.0)?\b", "Apache-2.0"),
    (r"\bapache\b.*\b1\.1\b", "Apache-1.1"),
    (r"\bmpl\b.*\b2(\.0)?\b|mozilla.*\b2(\.0)?\b", "MPL-2.0"),
    (r"\bmpl\b.*\b1\.1\b|mozilla.*\b1\.1\b", "MPL-1.1"),
    (r"\bepl\b.*\b2(\.0)?\b|eclipse public.*\b2(\.0)?\b", "EPL-2.0"),
    (r"\bepl\b.*\b1(\.0)?\b|eclipse public.*\b1(\.0)?\b", "EPL-1.0"),
    (r"\blgpl\b.*2\.1|lesser general public.*2\.1", "LGPL-2.1"),
    (r"\blgpl\b.*\b3\b|lesser general public.*\b3\b", "LGPL-3.0"),
    (r"\bgpl\b.*\b3\b|general public.*\b3\b", "GPL-3.0"),
    (r"\bgpl\b.*\b2\b|general public.*\b2\b", "GPL-2.0"),
    (r"\bcddl\b.*\b1\.1\b", "CDDL-1.1"),
    (r"\bcddl\b", "CDDL-1.0"),
    (r"\bmit\b", "MIT"),
    (r"bsd.*\b3\b|new bsd|modified bsd", "BSD-3-Clause"),
    (r"bsd.*\b2\b|simplified bsd", "BSD-2-Clause"),
    (r"\bbsd\b", "BSD"),
    (r"\bisc\b", "ISC"),
    (r"public domain|unlicense|\bcc0\b", "Public-Domain"),
]


def _match(s: str) -> str | None:
    """SPDX id for an already-normalised string, or None if no rule fits."""
    for pattern, spdx in _RULES:
        if re.search(pattern, s):
            return spdx
    return None


def _normalise(name: str) -> str:
    s = (name or "").strip().lower().replace("_", " ")
    # "GPLv2", "LGPL v2.1", "Apache v2" -> a space before the version, so the
    # \b in the rules can see it.
    s = re.sub(r"\b(l?gpl|epl|mpl|cddl|apache|asl)\s*v\.?\s*(?=\d)", r"\1 ", s)
    return re.sub(r"\s+", " ", s).strip().rstrip(".")


def canonical(name: str) -> str:
    """One licence name -> an SPDX-ish id, or a cleaned-up fallback."""
    s = _normalise(name)
    hit = _match(s)
    if hit:
        return hit
    # Unknown licence: keep it, normalised, so a genuine change is still
    # visible. Better an unrecognised id than a silent match.
    return re.sub(r"[^a-z0-9.+-]+", "-", s).strip("-").upper() or "UNKNOWN"


# "or", "/", ";", "+" separate two licences. A comma usually does not — it is
# far more often punctuation inside one name ("Apache License, Version 2.0"),
# so splitting on it eagerly turns a single licence into two unrecognised
# fragments. Comma is tried only as a fallback, and only when it yields parts
# that are each independently recognisable.
_STRONG = re.compile(r"\s+or\s+|\s*/\s*|\s*;\s*|\s*\+\s*", re.I)


def canonical_set(names: list[str]) -> set[str]:
    """A POM's licences as a set of ids.

    Dual licences appear both as one entry ("MPL 2.0 or EPL 1.0") and as two
    separate <license> elements, depending on who wrote the POM. Normalising
    both spellings to the same set is what stops every h2 bump from reporting
    a licence change.
    """
    out: set[str] = set()
    for n in names or []:
        for chunk in _STRONG.split(n or ""):
            chunk = chunk.strip()
            if not chunk:
                continue
            # A comma-joined pair ("MPL 2.0, EPL 1.0") is only read as two
            # licences when every part is independently recognised. This is
            # tried before matching the whole chunk, because the whole of
            # that example matches the MPL rule and would silently drop EPL.
            parts = [p.strip() for p in chunk.split(",") if p.strip()]
            hits = [_match(_normalise(p)) for p in parts] if len(parts) > 1 else []
            if hits and all(hits):
                out.update(hits)
                continue
            out.add(canonical(chunk))
    return out


def pom_licences(xml_text: str) -> list[str] | None:
    """Licence names from a POM, or None if it will not parse."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    names = [e.text.strip() for e in root.findall(".//m:licenses/m:license/m:name", POM_NS) if e.text]
    if not names:  # some POMs omit the namespace entirely
        names = [e.text.strip() for e in root.findall(".//licenses/license/name") if e.text]
    return names


def fetch(url: str, timeout: int = 60) -> str | None:
    try:
        with urllib.request.urlopen(url, context=_CTX, timeout=timeout) as f:
            return f.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — any failure means "not available here"
        return None


def gav_path(group: str, artifact: str, version: str) -> str:
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"


def parent_gav(xml_text: str) -> tuple[str, str, str] | None:
    """The <parent> coordinate of a POM, if it declares one."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    # `find(...) or find(...)` is wrong here: an Element with no children is
    # falsy, so test against None explicitly.
    p = root.find("m:parent", POM_NS)
    if p is None:
        p = root.find("parent")
    if p is None:
        return None
    vals = []
    for tag in ("groupId", "artifactId", "version"):
        e = p.find(f"m:{tag}", POM_NS)
        if e is None:
            e = p.find(tag)
        if e is None or not (e.text or "").strip():
            return None
        vals.append(e.text.strip())
    return vals[0], vals[1], vals[2]


def resolve_licences(base: str, repos: list[str], group: str, artifact: str,
                     version: str, depth: int = 4) -> tuple[list[str] | None, str]:
    """Licences for a coordinate, following <parent> the way Maven does.

    Most artifacts in this demo declare no <licenses> of their own —
    commons-text inherits Apache-2.0 from commons-parent. Reading only the
    artifact's own POM reports "no licence declared" for the one dependency
    the lab is built around, so walk up until a POM declares some.

    Returns (names, provenance). names is None only when nothing in the chain
    could be fetched at all.
    """
    g, a, v = group, artifact, version
    first_ok = False
    for hop in range(depth):
        text = None
        for repo in repos:
            text = fetch(f"{base}/repository/{repo}/{gav_path(g, a, v)}")
            if text is not None:
                break
        if text is None:
            # The starting POM is missing -> unknown. A missing *parent* just
            # ends the walk with whatever we already know.
            return (None, "") if hop == 0 else ([], "chain incomplete")
        first_ok = True
        names = pom_licences(text)
        if names is None:
            return (None, "") if hop == 0 else ([], "chain incomplete")
        if names:
            return names, ("declared" if hop == 0 else f"inherited from {g}:{a}:{v}")
        nxt = parent_gav(text)
        if not nxt:
            break
        g, a, v = nxt
    return ([], "no licence anywhere in the parent chain") if first_ok else (None, "")


def cmd_license(a: argparse.Namespace) -> dict:
    base = a.nexus_base.rstrip("/")
    hosted = [r.strip() for r in a.repo.split(",") if r.strip()]
    proxied = [r.strip() for r in a.proxy.split(",") if r.strip()]
    # The remediated build is in a hosted repo; its parent POMs and the
    # upstream baseline are in a proxy. Offer every repo for every hop rather
    # than guessing which one holds a given POM.
    new_names, new_src = resolve_licences(base, hosted + proxied,
                                          a.group, a.artifact, a.new_version)
    if new_names is None:
        return {"status": "failed",
                "details": f"could not read the POM for {a.artifact} {a.new_version} "
                           f"from any of: {', '.join(hosted + proxied)}"}

    old_names, _ = resolve_licences(base, proxied + hosted,
                                    a.group, a.artifact, a.old_version)

    new_ids = canonical_set(new_names)
    old_ids = canonical_set(old_names or [])
    shown = ", ".join(sorted(new_ids)) or "none declared"
    note = f" ({new_src})" if new_src and new_src != "declared" else ""

    if old_names is None:
        # Nothing to compare against. Still a real answer: report what the
        # remediated build declares, and say the comparison was not possible.
        if new_ids:
            return {"status": "passed",
                    "details": f"{shown}{note}; baseline POM {a.old_version} not retrievable, "
                               "so no change could be detected"}
        return {"status": "warning",
                "details": f"no licence found for {a.artifact} {a.new_version}, "
                           f"and baseline {a.old_version} was not retrievable"}

    if not new_ids and not old_ids:
        return {"status": "warning",
                "details": f"neither {a.old_version} nor {a.new_version} declares a licence "
                           "anywhere in its parent chain"}
    if not new_ids:
        return {"status": "warning",
                "details": f"{a.new_version} declares no licence "
                           f"(baseline {a.old_version}: {', '.join(sorted(old_ids))})"}
    if not old_ids:
        return {"status": "passed",
                "details": f"{shown}{note}; baseline {a.old_version} declared none"}
    if old_ids == new_ids:
        return {"status": "passed", "details": f"{shown}, unchanged from {a.old_version}{note}"}
    if new_ids >= old_ids:
        added = ", ".join(sorted(new_ids - old_ids))
        return {"status": "passed",
                "details": f"{shown}{note}; baseline licences retained, {added} additionally declared"}
    return {"status": "warning",
            "details": f"licence changed: {a.old_version} was {', '.join(sorted(old_ids))}, "
                       f"{a.new_version} is {shown}{note}"}


# --------------------------------------------------------------------------
# regression
#
# "Behavioural diff" with no baseline is unanswerable, which is why the agent
# used to invent it. Run the suite on the target branch as well and compare
# per-test outcomes: that is a measurement, and it is what the row claims.
# --------------------------------------------------------------------------
def read_results(d: str) -> dict[str, str] | None:
    """{classname.testname: outcome} from surefire/failsafe XML under d."""
    files = glob.glob(os.path.join(d, "**", "TEST-*.xml"), recursive=True)
    if not files:
        return None
    out: dict[str, str] = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            name = f"{tc.get('classname','?')}.{tc.get('name','?')}"
            outcome = "passed"
            for child in tc:
                tag = child.tag.lower()
                if tag in ("failure", "error"):
                    outcome = tag
                    break
                if tag == "skipped":
                    outcome = "skipped"
            out[name] = outcome
    return out


SUMMARY = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                     r"\s*Skipped:\s*(\d+)")


def read_log_section(lines: list[str]) -> tuple[int, int, int, int] | None:
    """Totals from the `Results:` blocks of one Maven run.

    Surefire prints a per-class `Tests run:` line for every test class and
    then one more under `Results:`; failsafe prints its own. Taking the last
    line in the section would report integration tests only, and taking every
    line would count each class twice — so count only lines under `Results:`,
    and sum them so unit and integration tests both land in the total.
    """
    totals = [0, 0, 0, 0]
    armed = 0
    seen = False
    for line in lines:
        if "Results:" in line:
            armed = 6
            continue
        if armed:
            armed -= 1
            m = SUMMARY.search(line)
            if m:
                seen = True
                armed = 0
                for i in range(4):
                    totals[i] += int(m.group(i + 1))
    return tuple(totals) if seen else None  # type: ignore[return-value]


def split_log(text: str) -> tuple[list[str], list[str]]:
    """The HEAD and BASE halves of a two-build Job log."""
    head: list[str] = []
    base: list[str] = []
    cur = None
    for line in text.splitlines():
        if "===LW-HEAD===" in line:
            cur = head
            continue
        if "===LW-BASE===" in line:
            cur = base
            continue
        if "===LW-END===" in line:
            cur = None
            continue
        if cur is not None:
            cur.append(line)
    return head, base


def cmd_regression(a: argparse.Namespace) -> dict:
    if getattr(a, "log", None):
        return regression_from_log(a.log)
    if not getattr(a, "base_dir", None):
        return {"status": "failed", "details": "--head-dir needs --base-dir to compare against"}

    base = read_results(a.base_dir)
    head = read_results(a.head_dir)

    if head is None:
        return {"status": "failed",
                "details": f"no test reports under {a.head_dir} — the suite did not run"}
    if base is None:
        return {"status": "warning",
                "details": (f"{len(head)} tests ran on the change, but the baseline suite produced "
                            "no reports, so no behavioural comparison was possible")}

    broke = sorted(n for n in base if base[n] == "passed" and head.get(n) in ("failure", "error"))
    fixed = sorted(n for n in base if base[n] in ("failure", "error") and head.get(n) == "passed")
    gone = sorted(set(base) - set(head))
    added = sorted(set(head) - set(base))

    if broke:
        shown = ", ".join(broke[:3]) + (f" and {len(broke)-3} more" if len(broke) > 3 else "")
        return {"status": "failed",
                "details": f"{len(broke)} test(s) passed on the base branch and fail with this change: {shown}"}
    if gone:
        shown = ", ".join(gone[:3]) + (f" and {len(gone)-3} more" if len(gone) > 3 else "")
        return {"status": "warning",
                "details": f"{len(gone)} test(s) present on the base branch no longer run: {shown}"}

    bits = [f"behavioural diff: 0 regressions across {len(head)} tests"]
    if fixed:
        bits.append(f"{len(fixed)} previously failing test(s) now pass")
    if added:
        bits.append(f"{len(added)} new test(s)")
    return {"status": "passed", "details": "; ".join(bits)}


def regression_from_log(path: str) -> dict:
    """Compare the two Maven runs recorded in a verify Job's log.

    A dependency bump changes only pom.xml, so both runs execute the same
    test code; the difference is which dependency version it ran against.
    That makes the suite totals a fair comparison, and totals survive in the
    log after the Job pod is gone — which the surefire XML does not.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return {"status": "failed", "details": f"could not read the Job log: {e}"}

    head_lines, base_lines = split_log(text)
    if not head_lines:
        return {"status": "failed",
                "details": "the Job log has no ===LW-HEAD=== section — the two-build "
                           "verify Job did not run as specified"}

    head = read_log_section(head_lines)
    base = read_log_section(base_lines)

    if head is None:
        return {"status": "failed",
                "details": "the build against the new version produced no test results"}
    h_run, h_fail, h_err, _ = head
    h_bad = h_fail + h_err

    if base is None:
        return {"status": "warning",
                "details": (f"{h_run} tests ran against the new version "
                            f"({h_bad} failures/errors), but the baseline build produced no "
                            "test results, so no behavioural comparison was possible")}
    b_run, b_fail, b_err, _ = base
    b_bad = b_fail + b_err

    if h_bad > b_bad:
        return {"status": "failed",
                "details": (f"{h_bad - b_bad} test(s) regressed: baseline had {b_bad} "
                            f"failures/errors across {b_run} tests, the new version has "
                            f"{h_bad} across {h_run}")}
    if h_run < b_run:
        return {"status": "warning",
                "details": (f"{b_run - h_run} fewer tests ran against the new version "
                            f"({h_run} vs {b_run}) — the comparison is incomplete")}
    extra = ""
    if h_bad < b_bad:
        extra = f"; {b_bad - h_bad} test(s) failing on the baseline now pass"
    return {"status": "passed",
            "details": (f"behavioural diff: 0 regressions across {h_run} tests, "
                        f"same outcome as the baseline build{extra}")}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    lic = sub.add_parser("license", help="compare baseline and remediated POM licences")
    lic.add_argument("--nexus-base", required=True, help="https://nexus-<ns>.<domain>")
    lic.add_argument("--group", required=True)
    lic.add_argument("--artifact", required=True)
    lic.add_argument("--old-version", required=True)
    lic.add_argument("--new-version", required=True)
    # Repo names differ between tenants, so accept candidates rather than one
    # name: every POM fetch tries each in turn. A wrong guess then costs a
    # 404 and the next attempt, instead of a bogus "licence unknown".
    lic.add_argument("--repo", default="lightwell-remediated",
                     help="hosted repo(s) holding the .rhlw build, comma-separated")
    lic.add_argument("--proxy", default="maven-public,maven-central",
                     help="proxy/group repo(s) holding the upstream baseline and parent POMs")
    lic.set_defaults(fn=cmd_license)

    reg = sub.add_parser("regression", help="diff test outcomes between base and change")
    src = reg.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", help="verify Job log containing ===LW-HEAD===/===LW-BASE=== sections")
    src.add_argument("--head-dir", help="merge-request surefire/failsafe reports")
    reg.add_argument("--base-dir", help="target-branch reports (with --head-dir)")
    reg.set_defaults(fn=cmd_regression)

    a = p.parse_args()
    print(json.dumps(a.fn(a), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
