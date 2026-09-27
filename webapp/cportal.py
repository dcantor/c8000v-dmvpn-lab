"""A customer's own view of the service: read-only, behind a link that only that customer is given.

The link (/c/<token>) carries a random token per customer, kept in customer_portal/tokens.json on the lab host (not in
git) and rotated from the operator's portal. Everything a customer page reads goes through /api/c/<token>/…, which
answers only about that customer: its site and host, the hubs and providers as its service sees them, its
applications and service levels, planned maintenance (change requests that touch it) and incidents (simulated
failures that touch it). Other customers never appear — not their names, addresses or shortcuts."""
import json
import secrets
import threading
from pathlib import Path

DIR = Path(__file__).resolve().parent / "customer_portal"
DIR.mkdir(exist_ok=True)
TOKENS = DIR / "tokens.json"
_lock = threading.Lock()


def _load():
    try:
        return json.loads(TOKENS.read_text())
    except (OSError, ValueError):
        return {}


def token_for(customer, rotate=False):
    with _lock:
        t = _load()
        if rotate or customer not in t:
            t[customer] = secrets.token_urlsafe(18)
            TOKENS.write_text(json.dumps(t, indent=1))
        return t[customer]


def customer_of(token):
    return next((c for c, t in _load().items() if secrets.compare_digest(t, token or "")), None)


def forget(customer):
    with _lock:
        t = _load()
        if t.pop(customer, None):
            TOKENS.write_text(json.dumps(t, indent=1))


def view(snap, c, faults, changes, kinds):
    """The state snapshot, reduced to what customer `c` may see."""
    nodes = snap["nodes"]
    host = nodes[c].get("host")
    hubs = snap["hubs"]
    provs = snap["provider"]["nodes"] + (((snap.get("provider2") or {}).get("nodes")) or [])
    keep = hubs + provs + [c] + ([host] if host else [])
    mine_ips = {(nodes[c].get(k) or {}).get("ip", "").split("/")[0] for k in ("wan", "wan2")}
    hub_ips = {(nodes[h].get(k) or {}).get("ip", "").split("/")[0] for h in hubs for k in ("wan", "wan2")}
    out_nodes = {}
    for n in keep:
        x = dict(nodes[n])
        if x["role"] == "provider":           # its ports would name every other customer
            x["ports"] = [p for p in x["ports"] if p.get("peer") in hubs + [c]]
        out_nodes[n] = x
    cloud = {}
    for n in hubs:
        s = dict((snap.get("cloud") or {}).get(n) or {})
        for k in ("registered", "registered2"):
            if k in s:
                s[k] = [x for x in s[k] if x == c]
        s.pop("nhrp", None)
        s.pop("sa_peers", None)
        cloud[n] = s
    mine = dict((snap.get("cloud") or {}).get(c) or {})
    mine["shortcuts"] = ["another site"] * len(mine.get("shortcuts") or [])
    mine.pop("nhrp", None)
    mine.pop("sa_peers", None)
    cloud[c] = mine
    pstate = {}
    for p, s in (snap.get("provider_state") or {}).items():
        s = dict(s)
        s["sessions"] = [x for x in s.get("sessions") or [] if x["peer"] in mine_ips | hub_ips]
        s.pop("interfaces", None)
        pstate[p] = s
    subs = set(((nodes[c].get("customer")) or {}).get("applications") or [])
    touching = lambda aff: "all" in aff or c in aff    # noqa: E731
    incidents = [{"title": kinds[x["kind"]]["title"], "since": x["started"],
                  "what": ("your site: " if c in x.get("nodes", []) or any(l.startswith(c + ":") for l in x.get("links", [])) else "the network: ")
                          + kinds[x["kind"]]["what"]}
                 for x in faults if _fault_touches(x, c, nodes, hubs)]
    planned = [{"id": cr["id"], "summary": cr["summary"] if c in cr.get("affects", []) else cr["title"] + " (network-wide)",
                "status": cr["status"], "scheduled_for": cr.get("scheduled_for"), "requested_at": cr["requested_at"]}
               for cr in changes if cr["status"] in ("pending", "scheduled") and touching(cr.get("affects") or [])]
    ok = not mine.get("error") and (mine.get("underlay_up") or 0) >= 1 and len(mine.get("nhs_up") or []) >= 1
    full = ok and len(mine.get("nhs_up") or []) >= len(hubs) and (snap.get("hosts_up") or {}).get(host)
    service = {**snap["service"], "spokes": [c]}           # the service's customer list would name every other customer
    return {"customer_view": True, "customer": c, "service": service, "provider": snap["provider"],
            "provider2": snap.get("provider2"), "oob": None, "hubs": hubs, "customers": [c], "nodes": out_nodes,
            "applications": [a for a in snap.get("applications") or [] if a["id"] in subs],
            "cloud": cloud, "provider_state": pstate, "hosts_up": {host: (snap.get("hosts_up") or {}).get(host)} if host else {},
            "health": {"ok": bool(full), "service": "up" if full else ("degraded" if ok else "down"), "problems": [],
                       "hubs": len(hubs), "customers": 1, "shortcuts": len(mine.get("shortcuts") or [])},
            "generated": snap.get("generated"), "live": snap.get("live"), "faults": [], "incidents": incidents, "planned": planned}


def _fault_touches(x, c, nodes, hubs):
    if x["kind"] in ("site-wan", "tunnel-down"):
        return x["target"].partition(":")[0] == c
    if x["kind"] == "provider-down":
        return any((nodes[c].get(k) or {}).get("peer") == x["target"] for k in ("wan", "wan2"))
    return True                                   # a hub: every customer registers with every hub
