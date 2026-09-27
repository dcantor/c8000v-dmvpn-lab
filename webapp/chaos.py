"""Failure simulation: take a piece of the lab down on purpose, see what the design does, and put it back.

Every fault is reversible and never saved on a router — a reboot, a Deploy or Fix drift also undoes it:

  hub-down        a hub fails: its VM is frozen (virsh suspend) — no packets in or out, no BGP, no NHRP
  hub-wan         a hub loses its link into the first provider (Gi2 shut)
  provider-down   a provider fails: its VM is frozen — every site loses that provider at once
  site-wan        a customer's circuit into a provider is cut (the provider's port to it is disabled)
  tunnel-down     a customer's first-cloud tunnel goes down (Tunnel0 / tun0 shut): IPsec and NHRP over it stop

Active faults live in faults/active.json, finished ones are appended to faults/history.jsonl. A fault left on is
restored by the portal after FAULT_MAX_MIN minutes.

Failover timing (`measure`) runs one fault as an experiment: every LAN host pings every other host and every hub's LAN
five times a second; the fault goes in; after a hold the fault comes out; the lab is given time to converge back.
Each flow's replies (busybox ping's seq numbers) give its outage: how long, when it began after the fault, whether the
flow came back while the fault was still in (failed over) or only after it was restored (isolated)."""
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import vyos_ssh  # noqa: E402

from state import HOST_PASS, HOST_USER, IOS_PASS, IOS_USER, _ssh  # noqa: E402

DIR = Path(__file__).resolve().parent / "faults"
DIR.mkdir(exist_ok=True)
ACTIVE = DIR / "active.json"
HISTORY = DIR / "history.jsonl"
RESULTS = Path(__file__).resolve().parent / "failover"
RESULTS.mkdir(exist_ok=True)
FAULT_MAX_MIN = float(os.environ.get("FAULT_MAX_MIN", "30"))
_lock = threading.Lock()

KINDS = {
    "hub-down": {"title": "Hub fails", "targets": "hubs",
                 "what": "the hub's VM is frozen: no packets, no BGP, no NHRP",
                 "expect": "customers keep two hubs; routes through it move to the others when BGP gives up on it (hold 9 s); "
                           "live shortcuts do not need the hub and stay up"},
    "hub-wan": {"title": "Hub loses its first provider", "targets": "hubs",
                "what": "the hub's Gi2 (into the first provider) is shut",
                "expect": "its first-cloud tunnels and sessions drop; the other hubs carry the first cloud; a dual-homed "
                          "customer still reaches the hub over the second cloud"},
    "provider-down": {"title": "Provider fails", "targets": "providers",
                      "what": "the provider's VM is frozen: every site loses its circuit into it at once",
                      "expect": "first provider: single-homed customers are cut off, dual-homed ones move to the second "
                                "cloud once BGP notices (hold 9 s). Second provider: nothing is lost — it is the backup"},
    "site-wan": {"title": "Customer circuit cut", "targets": "customer links",
                 "what": "the provider's port towards the customer is disabled",
                 "expect": "a single-homed customer is cut off; a dual-homed one moves to its other provider's cloud "
                           "(first circuit) or loses nothing (second circuit)"},
    "tunnel-down": {"title": "Customer tunnel down", "targets": "customers",
                    "what": "the customer's first-cloud tunnel (Tunnel0 / tun0) is shut",
                    "expect": "like a cut first circuit for the overlay: a dual-homed customer moves to cloud 2, a "
                              "single-homed one is cut off from the other sites"},
}


def _virsh(*args):
    p = subprocess.run(["sg", "libvirt", "-c", "virsh -q -c qemu:///system " + " ".join(args)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"virsh {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def _ios_config(ip, lines):
    from netmiko import ConnectHandler
    c = ConnectHandler(device_type="cisco_xe", host=ip, username=IOS_USER, password=IOS_PASS, fast_cli=False)
    try:
        return c.send_config_set(lines)
    finally:
        c.disconnect()


def catalog(f):
    """Every fault that can be applied to this lab, with its targets."""
    nodes = f["nodes"]
    provs = f["provider"]["nodes"] + ((f.get("provider2") or {}).get("nodes") or [])
    links = []
    for c in f["customers"]:
        for key, label in (("wan", "first"), ("wan2", "second")):
            w = nodes[c].get(key)
            if w:
                links.append({"id": f"{c}:{key}", "label": f"{c} ↔ {w['peer']} {w['peer_port']} ({label} provider)"})
    targets = {"hubs": [{"id": h, "label": h} for h in f["hubs"]],
               "providers": [{"id": p, "label": p + (" (second provider)" if p not in f["provider"]["nodes"] else "")} for p in provs],
               "customer links": links,
               "customers": [{"id": c, "label": c} for c in f["customers"]]}
    return [{"kind": k, **v, "options": targets[v["targets"]]} for k, v in KINDS.items()]


def active():
    try:
        return json.loads(ACTIVE.read_text())
    except (OSError, ValueError):
        return []


def _save(items):
    ACTIVE.write_text(json.dumps(items, indent=1))


def history(limit=30):
    try:
        lines = HISTORY.read_text().splitlines()[-limit:]
    except OSError:
        return []
    return [json.loads(l) for l in reversed(lines) if l.strip()]


def _describe(f, kind, target):
    nodes = f["nodes"]
    if kind in ("hub-down", "hub-wan"):
        if target not in f["hubs"]:
            raise ValueError(f"{target} is not a hub")
        return {"nodes": [target], "links": [f"{target}:wan"] if kind == "hub-wan" else []}
    if kind == "provider-down":
        provs = f["provider"]["nodes"] + ((f.get("provider2") or {}).get("nodes") or [])
        if target not in provs:
            raise ValueError(f"{target} is not a provider")
        return {"nodes": [target], "links": []}
    if kind == "site-wan":
        c, _, key = target.partition(":")
        if c not in f["customers"] or key not in ("wan", "wan2") or not nodes[c].get(key):
            raise ValueError(f"{target} is not a customer's provider link")
        return {"nodes": [c], "links": [target]}
    if kind == "tunnel-down":
        if target not in f["customers"]:
            raise ValueError(f"{target} is not a customer")
        return {"nodes": [target], "links": []}
    raise ValueError(f"unknown fault {kind}")


def _act(f, kind, target, undo):
    """Put the fault in (undo=False) or take it out (undo=True); returns what was done."""
    nodes = f["nodes"]
    if kind in ("hub-down", "provider-down"):
        _virsh("resume" if undo else "suspend", nodes[target]["domain"])
        return f"virsh {'resume' if undo else 'suspend'} {nodes[target]['domain']}"
    if kind == "hub-wan":
        _ios_config(nodes[target]["mgmt_ip"], ["interface GigabitEthernet2", "no shutdown" if undo else "shutdown"])
        return f"{target}: interface GigabitEthernet2 {'no shutdown' if undo else 'shutdown'}"
    if kind == "site-wan":
        c, _, key = target.partition(":")
        w = nodes[c][key]
        prov = nodes[w["peer"]]
        line = f"interfaces ethernet {w['peer_port']} disable"
        rc, out = vyos_ssh.configure(prov["mgmt_ip"], [("delete " if undo else "set ") + line], save=False)
        if rc != 0 or re.search(r"Commit failed|is not valid|Set failed", out):
            raise RuntimeError(f"{prov['name']}: {out[-300:]}")
        return f"{prov['name']}: {'delete' if undo else 'set'} {line}"
    if kind == "tunnel-down":
        n = nodes[target]
        if n.get("platform") == "vyos":
            rc, out = vyos_ssh.configure(n["mgmt_ip"], [("delete " if undo else "set ") + "interfaces tunnel tun0 disable"], save=False)
            if rc != 0 or re.search(r"Commit failed|is not valid|Set failed", out):
                raise RuntimeError(f"{target}: {out[-300:]}")
            return f"{target}: {'delete' if undo else 'set'} interfaces tunnel tun0 disable"
        _ios_config(n["mgmt_ip"], ["interface Tunnel0", "no shutdown" if undo else "shutdown"])
        return f"{target}: interface Tunnel0 {'no shutdown' if undo else 'shutdown'}"
    raise ValueError(f"unknown fault {kind}")


def apply(f, kind, target, by="operator", note=""):
    with _lock:
        cur = active()
        if any(x["kind"] == kind and x["target"] == target for x in cur):
            raise ValueError(f"{KINDS[kind]['title']} on {target} is already in")
        scope = _describe(f, kind, target)
        done = _act(f, kind, target, undo=False)
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "title": KINDS[kind]["title"], "target": target, "started": time.time(),
                "by": by, "note": note, "done": done, **scope}
        _save(cur + [item])
        return item


def restore(f, fid, reason="restored"):
    with _lock:
        cur = active()
        item = next((x for x in cur if x["id"] == fid), None)
        if item is None:
            raise KeyError(fid)
        undone = _act(f, item["kind"], item["target"], undo=True)
        _save([x for x in cur if x["id"] != fid])
        rec = {**item, "ended": time.time(), "undone": undone, "reason": reason}
        with HISTORY.open("a") as h:
            h.write(json.dumps(rec) + "\n")
        return rec


def restore_all(f, reason="restored"):
    out = []
    for x in active():
        try:
            out.append(restore(f, x["id"], reason))
        except Exception as e:                                      # noqa: BLE001
            out.append({**x, "error": str(e)})
    return out


def expire(f):
    """Faults older than FAULT_MAX_MIN are put back — a simulated failure is not meant to outlive the lesson."""
    for x in active():
        if time.time() - x["started"] > FAULT_MAX_MIN * 60:
            try:
                restore(f, x["id"], reason=f"expired after {FAULT_MAX_MIN:g} min")
            except Exception:                                      # noqa: BLE001
                pass


# ---- failover timing ------------------------------------------------------------------------------------------------
INTERVAL = 0.2


def flows(f):
    """Every host to every other host, and every host to every hub's LAN (where the applications are)."""
    nodes = f["nodes"]
    hosts = sorted(n for n, x in nodes.items() if x["role"] == "host")
    out = []
    for h in hosts:
        for o in hosts:
            if o != h:
                out.append({"src": h, "dst": o, "addr": nodes[o]["lan_ip"].split("/")[0], "kind": "site"})
        for hub in f["hubs"]:
            out.append({"src": h, "dst": hub, "addr": str(ipaddress.ip_network(nodes[hub]["lan"]).network_address + 1), "kind": "hub"})
    return out


def start_pings(f, tag, seconds):
    """Start every flow's ping in the background on its host; returns {host: when its pings started, portal clock}
    (the midpoint of the SSH exchange that started them — the host's own clock is never trusted)."""
    nodes = f["nodes"]
    by_host = {}
    for fl in flows(f):
        by_host.setdefault(fl["src"], []).append(fl)

    def one(h):
        # each ping's pid is kept, so collect() can stop exactly these (a `pkill -f <tag>` would match its own shell)
        cmds = [f"nohup ping -i {INTERVAL} -W 1 -w {int(seconds)} {fl['addr']} > /tmp/fo-{tag}-{fl['dst']}.log 2>&1 & echo $! >> /tmp/fo-{tag}.pids;"
                for fl in by_host[h]]
        t = time.time()
        _ssh(nodes[h]["mgmt_ip"], " ".join(cmds) + " sleep 0.2; echo started", HOST_USER, HOST_PASS, timeout=30)
        return h, (t + time.time()) / 2

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, by_host))


def collect(f, tag):
    nodes = f["nodes"]
    by_host = {}
    for fl in flows(f):
        by_host.setdefault(fl["src"], []).append(fl)

    def one(h):
        out = _ssh(nodes[h]["mgmt_ip"], f"kill -INT $(cat /tmp/fo-{tag}.pids) 2>/dev/null; sleep 0.5; "
                   f"for f in /tmp/fo-{tag}-*.log; do echo \"=== $f\"; grep -o 'seq=[0-9]*' $f | tr '\\n' ' '; echo; "
                   f"head -1 $f; done; rm -f /tmp/fo-{tag}-*.log /tmp/fo-{tag}.pids", HOST_USER, HOST_PASS, timeout=60)
        seqs = {}
        for block in out.split("=== ")[1:]:
            name, _, rest = block.partition("\n")
            dst = name.strip().rsplit(f"fo-{tag}-", 1)[-1].removesuffix(".log")
            seqs[dst] = sorted({int(x) for x in re.findall(r"seq=(\d+)", rest)})
        return h, seqs

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, by_host))


def analyse(f, seqs, starts, t_inject, t_restore, t_end):
    """Per flow: the outages (runs of missing replies) in seconds, placed on the portal's clock."""
    res = []
    for fl in flows(f):
        t_start = starts.get(fl["src"], t_inject)
        total = int((t_end - t_start - 0.5) / INTERVAL)          # t_end: when collection began (the pings were then stopped)
        got = set((seqs.get(fl["src"]) or {}).get(fl["dst"], []))
        if not got:
            res.append({**fl, "lost": None, "outages": [], "verdict": "no data"})
            continue
        first = min(got)
        last = max(max(got), total)
        outages, run = [], None
        for sq in range(first, last + 1):
            if sq not in got:
                run = [sq, sq] if run is None else [run[0], sq]
            elif run is not None:
                outages.append(run)
                run = None
        if run is not None:
            outages.append(run)
        at = lambda sq, _t=t_start: _t + sq * INTERVAL     # noqa: E731 — seq 0 went out when the host's pings started
        outs = [{"from": round(at(a) - t_inject, 1), "to": round(at(b + 1) - t_inject, 1), "seconds": round((b - a + 1) * INTERVAL, 1)}
                for a, b in outages if (b - a + 1) * INTERVAL >= 0.6]          # a lone lost ping is noise, not an outage
        during = [o for o in outs if o["from"] < t_restore - t_inject and o["to"] > 0]
        after = [o for o in outs if o["from"] >= t_restore - t_inject - 0.5]
        hold = t_restore - t_inject
        if not during and not after:
            verdict = "unaffected"
        elif during and any(o["to"] >= hold - 0.3 for o in during):
            verdict = "cut off"               # no replies from the outage until the fault was taken out
        elif during:
            verdict = "failed over"
        else:
            verdict = "hit on restore"
        res.append({**fl, "sent": last - first + 1, "lost": sum(o["seconds"] for o in outs), "outages": outs, "verdict": verdict,
                    "worst": max((o["seconds"] for o in during), default=0.0), "worst_restore": max((o["seconds"] for o in after), default=0.0)})
    return res


def summary(res):
    by = {}
    for r in res:
        by.setdefault(r["verdict"], []).append(r)
    fo = [r["worst"] for r in by.get("failed over", [])]
    fo.sort()
    return {"flows": len(res), "by_verdict": {k: len(v) for k, v in by.items()},
            "failover_worst": max(fo) if fo else None, "failover_median": fo[len(fo) // 2] if fo else None,
            "restore_worst": max((r.get("worst_restore") or 0 for r in res), default=0.0),
            "cut_off": sorted({f"{r['src']}→{r['dst']}" for r in by.get("cut off", [])})}


def save_result(rep):
    (RESULTS / f"{rep['run']}.json").write_text(json.dumps(rep, indent=1))


def results(limit=30):
    out = []
    for p in sorted(RESULTS.glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text())
            d.pop("flows", None)
            out.append(d)
        except ValueError:
            pass
    return out


def result(run_id):
    p = RESULTS / f"{run_id}.json"
    return json.loads(p.read_text()) if p.exists() and "/" not in run_id else None
