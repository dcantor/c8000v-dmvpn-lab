"""Maintenance: while approved work is under way, the lab knows it is on purpose.

A maintenance record opens when a disruptive job starts (remove, modify, restore, deploy, fix drift, a failover
measurement) or a failure is simulated, and closes when the job ends or the failure is restored. An operator can also
declare one by hand. While a record is open:

  - its routers are exported as lab_maintenance{router} = 1, and the lab's alert rules (lab-portal/monitoring) do not
    fire for them — including the routers of the customers it affects, whose registrations it disturbs;
  - the map and the Jobs page show it, and so do the portals of the customers it affects;
  - the SLA leaves it out: availability is measured over the time the customer was not under announced maintenance,
    as a service contract would (lab_maintenance_customer{customer}).

Records live in maintenance/active.json; closed ones are appended to maintenance/history.jsonl."""
import json
import threading
import time
import uuid
from pathlib import Path

DIR = Path(__file__).resolve().parent / "maintenance"
DIR.mkdir(exist_ok=True)
ACTIVE = DIR / "active.json"
HISTORY = DIR / "history.jsonl"
_lock = threading.Lock()


def active():
    try:
        return json.loads(ACTIVE.read_text())
    except (OSError, ValueError):
        return []


def history(limit=30):
    try:
        lines = HISTORY.read_text().splitlines()[-limit:]
    except OSError:
        return []
    return [json.loads(l) for l in reversed(lines) if l.strip()]


def scope(f, routers, customers):
    """Expand to every router and host whose alerts the work would trip: the routers themselves, and every router and
    host of the customers it affects ("all": the whole lab)."""
    nodes = f["nodes"]
    if "all" in customers or "all" in routers:
        return sorted(n for n, x in nodes.items() if x["role"] in ("hub", "spoke", "provider", "host")), sorted(f["customers"])
    custs = sorted(set(c for c in customers if c in nodes))
    muted = set(r for r in routers if r in nodes)
    for c in custs:
        muted.add(c)
        if nodes[c].get("host"):
            muted.add(nodes[c]["host"])
    return sorted(muted), custs


def open_(f, kind, ref, summary, routers, customers, by, change=None):
    muted, custs = scope(f, routers, customers)
    with _lock:
        cur = active()
        rec = {"id": uuid.uuid4().hex[:8], "kind": kind, "ref": ref, "summary": summary, "routers": sorted(set(routers)),
               "muted": muted, "customers": custs, "all": "all" in customers or "all" in routers, "by": by, "change": change,
               "started": time.time()}
        ACTIVE.write_text(json.dumps(cur + [rec], indent=1))
        return rec


def close(ref=None, mid=None, reason="done"):
    with _lock:
        cur = active()
        gone = [x for x in cur if (ref is not None and x["ref"] == ref) or (mid is not None and x["id"] == mid)]
        if not gone:
            return []
        ACTIVE.write_text(json.dumps([x for x in cur if x not in gone], indent=1))
        with HISTORY.open("a") as h:
            for x in gone:
                h.write(json.dumps({**x, "ended": time.time(), "reason": reason}) + "\n")
        return gone


def muted():
    return sorted({r for x in active() for r in x["muted"]})


def customers_in():
    return sorted({c for x in active() for c in x["customers"]})


def metrics(lab, metric_line, f):
    out = ["# HELP lab_maintenance 1 while the node is under announced maintenance: its alerts do not fire",
           "# TYPE lab_maintenance gauge",
           "# HELP lab_maintenance_customer 1 while the customer is under announced maintenance: excluded from its SLA",
           "# TYPE lab_maintenance_customer gauge"]
    m, cs = set(muted()), set(customers_in())
    for n, x in sorted(f["nodes"].items()):
        if x["role"] in ("hub", "spoke", "provider", "host"):
            out.append(metric_line("lab_maintenance", {"lab": lab, "router": n}, int(n in m)))
    for c in f["customers"]:
        out.append(metric_line("lab_maintenance_customer", {"lab": lab, "customer": c}, int(c in cs)))
    return out
