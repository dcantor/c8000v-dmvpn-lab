"""Configuration history: every router's configuration before and after each job that changes the lab.

A changing job (add, modify, remove or restore customers, deploy, fix drift) snapshots every router as its first step
and again when it ends — whatever the outcome, so a failed job shows what it had changed so far. The job page shows the
difference per router; a router's details list the jobs that changed it. Snapshots are kept in confighist/<job>/
(before/, after/, diff.json), the newest KEEP jobs.

Lines that change without anyone changing the configuration are left out of the comparison: IOS's timestamps
("Last configuration change at …", "NVRAM config last updated …"), byte counts, and VyOS's password hashes (it stores a
fresh one each time it is given a password)."""
import difflib
import json
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import vyos_ssh  # noqa: E402

from state import ios  # noqa: E402

DIR = Path(__file__).resolve().parent / "confighist"
DIR.mkdir(exist_ok=True)
KEEP = 150
NOISE = re.compile(r"^(! Last configuration change at|! NVRAM config last updated|Current configuration :|Building configuration)")
HASH = re.compile(r"(authentication encrypted-password )'?\S+'?")          # VyOS re-hashes a password it is given again


def _read(n):
    if (n.get("platform") or "c8000v") == "vyos" or n["role"] == "provider":
        out = vyos_ssh.op(n["mgmt_ip"], "show configuration commands", timeout=120)
        if "set system host-name" not in out:
            raise RuntimeError("could not read the configuration")
        return out
    return ios(n["mgmt_ip"], "show running-config")["show running-config"]


def snapshot(inv, job, which):
    """Every router's configuration into confighist/<job>/<which>/<router>.txt; returns {router: error} for the unread."""
    d = DIR / job / which
    d.mkdir(parents=True, exist_ok=True)
    routers = [n for n in inv["nodes"] if n["role"] in ("hub", "spoke", "provider")]
    errors = {}

    def one(n):
        try:
            text = "\n".join(HASH.sub(r"\1<hash>", l) for l in _read(n).splitlines() if not NOISE.match(l)).strip() + "\n"
            (d / f"{n['name']}.txt").write_text(text)
        except Exception as e:                                        # noqa: BLE001
            errors[n["name"]] = f"{e.__class__.__name__}: {e}"[:200]

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, routers))
    return errors


def compare(job):
    """before vs after, per router: lines added and removed, and a unified diff. Written to diff.json."""
    base = DIR / job
    b, a = base / "before", base / "after"
    names = sorted({p.stem for p in b.glob("*.txt")} | {p.stem for p in a.glob("*.txt")})
    out = {}
    for n in names:
        old = (b / f"{n}.txt").read_text().splitlines() if (b / f"{n}.txt").exists() else []
        new = (a / f"{n}.txt").read_text().splitlines() if (a / f"{n}.txt").exists() else []
        if old == new:
            continue
        diff = list(difflib.unified_diff(old, new, f"{n} before", f"{n} after", n=2, lineterm=""))
        out[n] = {"added": sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
                  "removed": sum(1 for l in diff if l.startswith("-") and not l.startswith("---")),
                  "new": not old, "gone": not new, "diff": "\n".join(diff[:4000])}
    (base / "diff.json").write_text(json.dumps(out, indent=1))
    _prune()
    return out


def diff(job):
    p = DIR / job / "diff.json"
    return json.loads(p.read_text()) if "/" not in job and p.exists() else None


def history(router=None, limit=50):
    """Jobs that changed the lab's configuration (or one router's), newest first."""
    out = []
    for p in sorted(DIR.glob("*/diff.json"), reverse=True):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        if router and router not in d:
            continue
        out.append({"job": p.parent.name, "routers": {r: {k: v[k] for k in ("added", "removed", "new", "gone")} for r, v in d.items()}})
        if len(out) >= limit:
            break
    return out


def config(job, router, which):
    p = DIR / job / which / f"{router}.txt"
    if "/" in job + router or which not in ("before", "after") or not p.exists():
        return None
    return p.read_text()


def _prune():
    jobs = sorted(p for p in DIR.iterdir() if p.is_dir())
    for p in jobs[:-KEEP]:
        shutil.rmtree(p, ignore_errors=True)
