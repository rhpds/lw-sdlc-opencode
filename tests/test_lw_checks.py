#!/usr/bin/env python3
"""Regression guard for scripts/lw_checks.py.

  python3 -I tests/test_lw_checks.py

No pytest: this has to be runnable inside the agent container, which ships
nothing beyond the standard library.

The cases below are the ones that actually bit. Licence strings in real POMs
are free text, so the risk runs both ways — over-eager splitting shredded
"Apache License, Version 2.0" into two unrecognised fragments, and matching
the whole string first made "MPL 2.0, EPL 1.0" resolve to MPL alone.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lw_checks", ROOT / "scripts" / "lw_checks.py")
lw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lw)

failures = []


class NS:
    """Stand-in for the argparse namespace the cmd_* functions take."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got}\n     want: {want}")


def ids(s):
    return sorted(lw.canonical_set([s]))


# -- every spelling of Apache 2 that appears in the demo's POMs -------------
for s in ["Apache-2.0", "Apache License, Version 2.0", "apache2", "Apache 2",
          "APACHE_2.0", "Apache v2", "ASL 2.0", "apache2-something",
          "The Apache Software License, Version 2.0"]:
    check(f"apache: {s!r}", ids(s), ["Apache-2.0"])

# -- dual licences, however they are punctuated ----------------------------
dual = ["EPL-1.0", "MPL-2.0"]
check("dual 'or'", ids("MPL 2.0 or EPL 1.0"), dual)
check("dual comma", ids("MPL 2.0, EPL 1.0"), dual)
check("dual slash", ids("MPL 2.0 / EPL 1.0"), dual)
check("dual elements", sorted(lw.canonical_set(["MPL 2.0", "EPL 1.0"])), dual)
check("cddl+gpl", ids("CDDL + GPLv2 with classpath exception"), ["CDDL-1.0", "GPL-2.0"])

# -- a comma inside one name is not a separator ----------------------------
check("lgpl long form", ids("GNU Lesser General Public License, Version 2.1"), ["LGPL-2.1"])
check("epl long form", ids("Eclipse Public License, Version 2.0"), ["EPL-2.0"])

# -- the rest --------------------------------------------------------------
check("mit", ids("The MIT License (MIT)"), ["MIT"])
check("bsd3", ids("New BSD License"), ["BSD-3-Clause"])
check("empty", ids(""), [])

# -- versions and families must stay distinct ------------------------------
assert ids("GPLv2") != ids("GPLv3"), "GPL 2 and 3 collapsed"
assert ids("MIT") != ids("Apache-2.0"), "MIT and Apache collapsed"
assert ids("EPL 1.0") != ids("EPL 2.0"), "EPL 1 and 2 collapsed"
assert ids("Bouncy Castle Licence") == ["BOUNCY-CASTLE-LICENCE"], "unknown licence lost"

# -- POM parsing -----------------------------------------------------------
POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <licenses>
    <license><name>Apache License, Version 2.0</name></license>
  </licenses>
</project>"""
check("namespaced pom", lw.pom_licences(POM), ["Apache License, Version 2.0"])
check("bare pom", lw.pom_licences(POM.replace(' xmlns="http://maven.apache.org/POM/4.0.0"', "")),
      ["Apache License, Version 2.0"])
check("no licences", lw.pom_licences("<project/>"), [])
check("bad xml", lw.pom_licences("not xml <<<"), None)



# -- parent chain ----------------------------------------------------------
# commons-text declares no <licenses>; it inherits Apache-2.0 from its
# grandparent. Reading only the artifact's own POM would report "no licence
# declared" for the dependency the whole lab is built around.
def pom(parent=None, lic=None):
    p = (f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId>"
         f"<version>{parent[2]}</version></parent>") if parent else ""
    l = f"<licenses><license><name>{lic}</name></license></licenses>" if lic else ""
    return f'<project xmlns="http://maven.apache.org/POM/4.0.0">{p}{l}</project>'


REPO = {
    "org/apache/commons/commons-text/1.9/commons-text-1.9.pom":
        pom(parent=("org.apache.commons", "commons-parent", "52")),
    "org/apache/commons/commons-parent/52/commons-parent-52.pom":
        pom(parent=("org.apache", "apache", "23")),
    "org/apache/apache/23/apache-23.pom": pom(lic="Apache License, Version 2.0"),
    "org/lonely/orphan/1.0/orphan-1.0.pom": pom(),
}


def stub_fetch(url, timeout=60):
    for path, body in REPO.items():
        if url.endswith(path):
            return body
    return None


lw.fetch = stub_fetch

names, src = lw.resolve_licences("https://n", ["r"], "org.apache.commons", "commons-text", "1.9")
check("inherited licence", sorted(lw.canonical_set(names)), ["Apache-2.0"])
assert "inherited from org.apache:apache:23" == src, src
check("no licence in chain",
      lw.resolve_licences("https://n", ["r"], "org.lonely", "orphan", "1.0")[0], [])
check("missing pom", lw.resolve_licences("https://n", ["r"], "no.such", "thing", "9")[0], None)

# -- cmd_license verdicts --------------------------------------------------
REPO["org/apache/commons/commons-text/1.9.rhlw-00005/commons-text-1.9.rhlw-00005.pom"] = \
    pom(parent=("org.apache.commons", "commons-parent", "52"))


def lic(new="1.9.rhlw-00005", old="1.9", group="org.apache.commons", artifact="commons-text"):
    return lw.cmd_license(NS(nexus_base="https://n", group=group, artifact=artifact,
                             old_version=old, new_version=new,
                             repo="lightwell-remediated", proxy="maven-public"))


r = lic()
check("remediated build keeps Apache-2.0", r["status"], "passed")
assert "Apache-2.0" in r["details"], r["details"]

REPO["org/apache/commons/commons-text/1.9.rhlw-00006/commons-text-1.9.rhlw-00006.pom"] = \
    pom(lic="GPLv3")
r = lic(new="1.9.rhlw-00006")
check("licence swapped to GPL -> warning", r["status"], "warning")
assert "GPL-3.0" in r["details"], r["details"]

r = lic(new="1.9.rhlw-99999")
check("remediated pom missing -> failed", r["status"], "failed")


# -- regression: surefire diffing ------------------------------------------
def surefire(d, cases):
    os.makedirs(d, exist_ok=True)
    body = "".join(
        f'<testcase classname="C" name="{n}">{"" if o == "passed" else f"<{o}/>"}</testcase>'
        for n, o in cases.items())
    pathlib.Path(d, "TEST-C.xml").write_text(f'<testsuite>{body}</testsuite>')


with tempfile.TemporaryDirectory() as tmp:
    base, head = os.path.join(tmp, "base"), os.path.join(tmp, "head")

    surefire(base, {"a": "passed", "b": "passed"})
    surefire(head, {"a": "passed", "b": "passed"})
    check("clean run", lw.cmd_regression(NS(base_dir=base, head_dir=head))["status"], "passed")

    surefire(head, {"a": "passed", "b": "failure"})
    r = lw.cmd_regression(NS(base_dir=base, head_dir=head))
    check("new failure -> failed", r["status"], "failed")
    assert "C.b" in r["details"], f"failing test not named: {r['details']}"

    surefire(head, {"a": "passed"})
    check("dropped test -> warning",
          lw.cmd_regression(NS(base_dir=base, head_dir=head))["status"], "warning")

    surefire(base, {"a": "failure", "b": "passed"})
    surefire(head, {"a": "passed", "b": "passed"})
    check("base failure now fixed -> passed",
          lw.cmd_regression(NS(base_dir=base, head_dir=head))["status"], "passed")

    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    check("no baseline -> warning",
          lw.cmd_regression(NS(base_dir=empty, head_dir=head))["status"], "warning")
    check("no head reports -> failed",
          lw.cmd_regression(NS(base_dir=base, head_dir=empty))["status"], "failed")


# -- regression from a Job log --------------------------------------------
# The Job pod is gone by the time the agent reports, so the log is the only
# surviving evidence. Surefire prints a `Tests run:` line per class as well
# as under `Results:`; only the latter may be counted, or every class is
# double-counted.
def mvnlog(unit, ifails=None):
    run, fail, err = unit
    out = ["[INFO] Running com.example.ATest",
           "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0",
           "[INFO] Results:", "[INFO] ",
           f"[INFO] Tests run: {run}, Failures: {fail}, Errors: {err}, Skipped: 0"]
    if ifails is not None:
        irun, ifail, ierr = ifails
        out += ["[INFO] Results:", "[INFO] ",
                f"[INFO] Tests run: {irun}, Failures: {ifail}, Errors: {ierr}, Skipped: 0"]
    return "\n".join(out)


def joblog(head, base, ihead=None, ibase=None):
    parts = ["===LW-HEAD===", mvnlog(head, ihead)]
    if base is not None:
        parts += ["===LW-BASE===", mvnlog(base, ibase)]
    parts.append("===LW-END===")
    return "\n".join(parts)


with tempfile.TemporaryDirectory() as tmp:
    def rg(text):
        p = os.path.join(tmp, "job.log")
        pathlib.Path(p).write_text(text)
        return lw.regression_from_log(p)

    r = rg(joblog((42, 0, 0), (42, 0, 0)))
    check("log: clean", r["status"], "passed")
    assert "42 tests" in r["details"], r["details"]

    check("log: per-class lines not double counted",
          lw.read_log_section(mvnlog((42, 0, 0)).splitlines()), (42, 0, 0, 0))
    check("log: unit+integration summed",
          lw.read_log_section(mvnlog((42, 0, 0), (8, 0, 0)).splitlines()), (50, 0, 0, 0))

    r = rg(joblog((42, 2, 0), (42, 0, 0)))
    check("log: new failures -> failed", r["status"], "failed")
    assert "2 test(s) regressed" in r["details"], r["details"]

    check("log: pre-existing failures are not a regression",
          rg(joblog((42, 2, 0), (42, 2, 0)))["status"], "passed")
    r = rg(joblog((42, 0, 0), (42, 3, 0)))
    check("log: baseline failures now fixed", r["status"], "passed")
    assert "now pass" in r["details"], r["details"]

    check("log: no baseline -> warning", rg(joblog((42, 0, 0), None))["status"], "warning")
    check("log: fewer tests ran -> warning",
          rg(joblog((30, 0, 0), (42, 0, 0)))["status"], "warning")
    check("log: no head section -> failed", rg("nothing useful here")["status"], "failed")
    check("log: head ran nothing -> failed",
          rg("===LW-HEAD===\nno tests at all\n===LW-END===")["status"], "failed")
    check("log: missing file -> failed",
          lw.regression_from_log(os.path.join(tmp, "nope.log"))["status"], "failed")

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ok")
