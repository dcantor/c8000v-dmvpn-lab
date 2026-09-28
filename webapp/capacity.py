"""Capacity: how loaded each hub is, and how much room the lab has left for customers.

Per hub, read over SSH (at most every CACHE_S seconds):
- spokes: customers registered, against the planning figure (policy.json: hub_max_spokes);
- IPsec sessions: active against the platform's maximum (show crypto eli);
- control-plane CPU and DRAM: against the router's own warning and critical levels (show platform resources);
- WAN traffic: the 5-minute rates on the provider links, against the licensed throughput level
  (show platform hardware throughput level). An unlicensed C8000v is throttled, so this is often the first limit.

For the lab:
- each provider's customer ports (eth4-eth11), free and used;
- the lab host's memory: MemAvailable, less a reserve, against what one more customer costs (router + LAN host);
- the lab host's CPUs: running VMs' vCPUs against its cores, and how busy it is.

"Room to grow" is the smallest of those, for a C8000v customer and for a VyOS one, with the limit that runs out first.
A resource at or above `warn_pct` of its limit (or the router's own warning level) is a warning, at `crit_pct`
(or the router's critical level) critical."""
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from state import ios

DIR = Path(__file__).resolve().parent / "capacity"
DIR.mkdir(exist_ok=True)
POLICY = DIR / "policy.json"
CACHE_S = 120
DEFAULT = {
    "hub_max_spokes": 40,          # a planning figure for a 2-vCPU, 4 GB Catalyst 8000v hub: set it for your platform
    "warn_pct": 75,
    "crit_pct": 90,
    "host_reserve_mib": 4096,      # memory the lab host keeps for itself and everything else
}
_lock = threading.Lock()
_cache = {"at": 0.0, "hubs": {}}


def policy():
    try:
        return {**DEFAULT, **json.loads(POLICY.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT)


def validate_policy(p):
    probs = []
    for k in DEFAULT:
        v = p.get(k)
        if not isinstance(v, (int, float)) or v < 0:
            probs.append(f"{k} must be a number ≥ 0")
    if not probs:
        if p["hub_max_spokes"] < 1:
            probs.append("hub_max_spokes must be at least 1")
        if not 0 < p["warn_pct"] < p["crit_pct"] <= 100:
            probs.append("0 < warn_pct < crit_pct ≤ 100")
    return probs


def save_policy(p):
    POLICY.write_text(json.dumps({k: p[k] for k in DEFAULT}, indent=1))


# ---- reading a hub ----------------------------------------------------------------------------------------------------
RES = re.compile(r"^\s*(Control Processor|DRAM)\s+([\d.]+)(%|MB\(\d+%\))\s+(\S+)\s+(\d+)%\s+(\d+)%", re.M)


def _hub_platform(n, wan_ifs):
    cmds = ["show platform resources", "show platform hardware throughput level", "show crypto eli"] + \
           [f"show interfaces {i} | include rate" for i in wan_ifs]
    r = ios(n["mgmt_ip"], *cmds)
    out = {}
    rp = r["show platform resources"].split("ESP0")[0]                # the route processor's rows, not the QFP's
    for name, val, unit, mx, warn, crit in RES.findall(rp):
        if name == "Control Processor":
            out["cpu"] = {"used": float(val), "max": 100.0, "warn": int(warn), "crit": int(crit)}
        else:
            used_mb = float(val); max_mb = float(re.sub(r"\D", "", mx) or 0)
            out["dram"] = {"used": used_mb, "max": max_mb, "warn": int(warn), "crit": int(crit)}
    m = re.search(r"throughput level is (\d+) kb/s", r["show platform hardware throughput level"])
    out["throughput_kbps"] = int(m[1]) if m else None
    m = re.search(r"IPSec-Session\s*:\s*(\d+) active,\s*(\d+) max", r["show crypto eli"])
    out["ipsec"] = {"used": int(m[1]), "max": int(m[2])} if m else None
    bps = 0
    for i in wan_ifs:
        t = r.get(f"show interfaces {i} | include rate", "")
        for d, v in re.findall(r"5 minute (input|output) rate (\d+) bits/sec", t):
            bps += int(v)
    out["wan_bps"] = bps
    return out


def _hub_details(facts, hubs):
    """{hub: platform readings}, cached CACHE_S seconds; a hub that cannot be read keeps its error."""
    with _lock:
        if time.time() - _cache["at"] < CACHE_S and set(_cache["hubs"]) >= set(hubs):
            return _cache["hubs"]
    wan = lambda h: [p for p in ("GigabitEthernet2", "GigabitEthernet4") if any(
        x.get("name") == p and x.get("peer") for x in facts["nodes"][h].get("ports", []))] or ["GigabitEthernet2"]

    def one(h):
        try:
            return h, _hub_platform(facts["nodes"][h], wan(h))
        except Exception as e:                                          # noqa: BLE001
            return h, {"error": f"{e.__class__.__name__}: {e}"[:160]}

    with ThreadPoolExecutor(max_workers=4) as ex:
        got = dict(ex.map(one, hubs))
    with _lock:
        _cache.update(at=time.time(), hubs=got)
    return got


# ---- the lab host ----------------------------------------------------------------------------------------------------
def _meminfo():
    kv = {}
    for l in Path("/proc/meminfo").read_text().splitlines():
        k, v = l.split(":", 1)
        kv[k] = int(v.split()[0])
    return kv


def _cpu_busy(sample=0.4):
    def read():
        f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        return sum(f), f[3] + f[4]
    t1, i1 = read(); time.sleep(sample); t2, i2 = read()
    return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def _vm_vcpus():
    """(running VMs, their vCPUs) on the host, all labs: one virsh domstats call."""
    try:
        out = subprocess.run(["sg", "libvirt", "-c", "virsh -c qemu:///system domstats --list-running --vcpu"],
                             capture_output=True, text=True, timeout=30).stdout
        vcpus = [int(x) for x in re.findall(r"vcpu\.current=(\d+)", out)]
        return len(vcpus), sum(vcpus)
    except Exception:                                                   # noqa: BLE001
        return None, None


def _nproc():
    return len(re.findall(r"^processor", Path("/proc/cpuinfo").read_text(), re.M))


# ---- the picture -----------------------------------------------------------------------------------------------------
def _grade(pct, warn, crit):
    if pct is None:
        return "unknown"
    return "critical" if pct >= crit else "warning" if pct >= warn else "ok"


def _res(name, used, mx, unit, warn, crit, note=""):
    pct = round(100 * used / mx, 1) if mx else None
    return {"name": name, "used": used, "max": mx, "unit": unit, "pct": pct, "warn": warn, "crit": crit,
            "state": _grade(pct, warn, crit), "note": note}


WORST = {"unknown": 0, "ok": 1, "warning": 2, "critical": 3}


def compute(facts, snap, lab_conf):
    """Everything the Capacity view shows. `snap` is the live state (registrations); `lab_conf` the VM sizes."""
    p = policy()
    W, C = p["warn_pct"], p["crit_pct"]
    hubs = facts["hubs"]
    cloud = (snap or {}).get("cloud") or {}
    det = _hub_details(facts, hubs)
    out_hubs = []
    for h in hubs:
        st, d = cloud.get(h) or {}, det.get(h) or {}
        spokes = sorted(set(st.get("registered") or []) | set(st.get("registered2") or []))
        res = [_res("Spokes registered", len(spokes), p["hub_max_spokes"], "", W, C, "planning figure (editable)")]
        if d.get("ipsec"):
            res.append(_res("IPsec sessions", d["ipsec"]["used"], d["ipsec"]["max"], "", W, C, "platform maximum"))
        if d.get("cpu"):
            c = d["cpu"]; res.append(_res("Control-plane CPU", c["used"], 100, "%", c["warn"], c["crit"], "the router's own levels"))
        if d.get("dram"):
            m = d["dram"]; res.append(_res("DRAM", m["used"], m["max"], "MB", m["warn"], m["crit"], "the router's own levels"))
        if d.get("throughput_kbps"):
            res.append(_res("WAN traffic", round(d["wan_bps"] / 1000, 1), d["throughput_kbps"], "kb/s", W, C,
                            "5-minute rate, in + out, against the licensed throughput level"))
        state = max((r["state"] for r in res), key=WORST.get) if res else "unknown"
        out_hubs.append({"name": h, "region": facts["nodes"][h].get("region"), "spokes": spokes, "resources": res,
                         "state": "unknown" if d.get("error") else state, "error": d.get("error"),
                         "throughput_kbps": d.get("throughput_kbps")})

    # providers: customer ports eth4-eth11
    provs = []
    for which in ("provider", "provider2"):
        if not facts.get(which):
            continue
        for pn in facts[which]["nodes"]:
            ports = [x for x in facts["nodes"][pn]["ports"] if 4 <= x.get("num", 0) <= 11]
            used = [x["peer"] for x in ports if x.get("peer")]
            provs.append({"name": pn, "which": which, "used": used, "free": len(ports) - len(used), "total": len(ports),
                          "resource": _res("Customer ports", len(used), len(ports), "", W, C, "eth4-eth11")})

    # the lab host
    mem = _meminfo()
    avail = mem["MemAvailable"] // 1024
    total = mem["MemTotal"] // 1024
    vms, vcpus = _vm_vcpus()
    cores = _nproc()
    busy = _cpu_busy()
    cost = {"c8000v": lab_conf["C8000V_RAM_MIB"] + lab_conf["HOST_RAM_MIB"],
            "vyos": lab_conf["VYOS_RAM_MIB"] + lab_conf["HOST_RAM_MIB"]}
    host = {"mem_total_mib": total, "mem_available_mib": avail, "reserve_mib": p["host_reserve_mib"],
            "cores": cores, "vms_running": vms, "vcpus_allocated": vcpus, "cpu_busy_pct": busy, "customer_cost_mib": cost,
            "resources": [_res("Memory in use", total - avail, total, "MiB", W, C),
                          _res("CPU busy", busy, 100, "%", W, C, "all cores, now")]}
    if vcpus is not None:
        host["resources"].append(_res("vCPUs allocated", vcpus, cores, "", 100, 200, "running VMs; above 100% is overcommit"))

    # room to grow: what runs out first, per platform
    spare_mem = max(0, avail - p["host_reserve_mib"])
    room = {}
    for plat in ("c8000v", "vyos"):
        limits = {"memory on the lab host": spare_mem // cost[plat]}
        mp = [x for x in provs if x["which"] == "provider"]
        if mp:
            limits["the provider's customer ports"] = sum(x["free"] for x in mp)
        limits["hub capacity"] = max(0, min(p["hub_max_spokes"] - len(x["spokes"]) for x in out_hubs)) if out_hubs else 0
        first = min(limits, key=limits.get)
        room[plat] = {"customers": limits[first], "limited_by": first, "limits": limits}
    room["dual_homed_ports"] = sum(x["free"] for x in provs if x["which"] == "provider2") if any(
        x["which"] == "provider2" for x in provs) else None

    everything = [r for h in out_hubs for r in h["resources"]] + [x["resource"] for x in provs] + host["resources"]
    worst = max((r["state"] for r in everything), key=WORST.get, default="unknown")
    warnings = [f"{h['name']}: {r['name']} at {r['pct']}%" for h in out_hubs for r in h["resources"] if r["state"] in ("warning", "critical")]
    warnings += [f"{x['name']}: {x['free']} customer port(s) left" for x in provs if x["resource"]["state"] in ("warning", "critical")]
    warnings += [f"lab host: {r['name']} at {r['pct']}%" for r in host["resources"] if r["state"] in ("warning", "critical")]
    if room["c8000v"]["customers"] == 0:
        warnings.append(f"no room for another C8000v customer: {room['c8000v']['limited_by']}")
    return {"generated": time.time(), "policy": p, "state": worst, "hubs": out_hubs, "providers": provs, "host": host,
            "room": room, "warnings": warnings}


def plan_note(cap, platform, dual):
    """What adding one customer does to capacity, for the Add-a-customer plan: (text, problem or None)."""
    r = cap["room"].get(platform) or {}
    if r.get("customers", 1) < 1:
        return None, f"no room for another {platform} customer: {r['limited_by']}"
    if dual and cap["room"].get("dual_homed_ports") == 0:
        return None, "the second provider has no free customer port"
    busiest = max(cap["hubs"], key=lambda h: len(h["spokes"]), default=None)
    after = f"{busiest['name']} {len(busiest['spokes']) + 1}/{cap['policy']['hub_max_spokes']} spokes; " if busiest else ""
    text = (f"after this: {after}room for {r['customers'] - 1} more {platform} customer(s), limited by {r['limited_by']}")
    return text, None
