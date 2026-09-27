"""Live state of the cloud, collected from the routers themselves over SSH.

The model says what should be true; this says what is. Everything here is read: `show dmvpn` for NHRP (registrations
on a hub, NHS and shortcuts on a customer), `show crypto session brief` for IPsec, `show ip bgp summary` for both BGP
planes, the provider's FRR for its eBGP customers, and an SSH login on each LAN host. Collection is parallel and
cached for a few seconds, because the UI polls."""
import ipaddress
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import paramiko
from netmiko import ConnectHandler

import customers as C

IOS_USER, IOS_PASS = "admin", "admin"
VYOS_USER, VYOS_PASS = "vyos", "vyos"
HOST_USER, HOST_PASS = "lab", "lab"
DMVPN_ROW = re.compile(r"^\s*\d+\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", re.M)
BGP_ROW = re.compile(r"^\*?(\d+\.\d+\.\d+\.\d+)\s+4\s+(\d+)(?:\s+\d+){5}\s+\S+\s+(\S+)\s*$", re.M)
# FRR adds PfxSnt and Desc after State/PfxRcd
FRR_ROW = re.compile(r"^\*?(\d+\.\d+\.\d+\.\d+)\s+4\s+(\d+)(?:\s+\d+){5}\s+\S+\s+(\S+)", re.M)


def _ssh(host, cmd, user, password, timeout=30):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username=user, password=password, timeout=15, look_for_keys=False, allow_agent=False)
        _, out, err = c.exec_command(cmd, timeout=timeout)
        return out.read().decode(errors="replace") + err.read().decode(errors="replace")
    finally:
        c.close()


def ios(ip, *commands):
    """Exec commands on a C8000v; returns {command: output}."""
    c = ConnectHandler(device_type="cisco_xe", host=ip, username=IOS_USER, password=IOS_PASS, fast_cli=False)
    try:
        return {cmd: c.send_command(cmd, read_timeout=60) for cmd in commands}
    finally:
        c.disconnect()


def vyos_show(ip, command):
    """A `show` command on a VyOS router: NHRP, BGP and routes live in FRR (vtysh), IPsec and the rest in op-mode."""
    if command.startswith("show vpn") or command.startswith("show interfaces") or command.startswith("show configuration"):
        return vyos_op(ip, command)
    return vtysh(ip, command)


def vyos_op(ip, command, timeout=90):
    """A VyOS op-mode command over a plain SSH exec channel, through VyOS's own op-mode wrapper — no interactive
    prompt to detect (netmiko's VyOS login intermittently misses it and times out)."""
    safe = command.replace("'", "")
    return _ssh(ip, f"/opt/vyatta/bin/vyatta-op-cmd-wrapper {safe}", VYOS_USER, VYOS_PASS, timeout=timeout)


def vtysh(ip, *commands):
    return _ssh(ip, " ; ".join(f"sudo vtysh -c '{c}'" for c in commands), VYOS_USER, VYOS_PASS)


class State:
    def __init__(self, ttl=15):
        # one cache slot per `live` flag: a model-only snapshot is never served to a caller that asked for live state
        self.ttl, self._cache, self._lock = ttl, {}, threading.Lock()

    def model(self):
        f = C.facts()
        inv = f["inv"]
        return {"service": inv["service"], "provider": inv["provider"], "oob": inv["oob"], "hubs": f["hubs"],
                "customers": f["customers"], "nodes": {n["name"]: n for n in inv["nodes"]},
                "applications": inv.get("applications") or []}

    # ---- per router --------------------------------------------------------------------------------------------
    def vyos_router_state(self, node, n, m):
        """A VyOS customer, from FRR (nhrpd, bgpd) and strongSwan: the same fields the IOS parser produces."""
        out = {"name": node, "role": n["role"], "platform": "vyos", "error": None, "tunnel_ip": n["tunnel_ip"], "nbma": n["nbma"]}
        try:
            text = vtysh(n["mgmt_ip"], "show ip nhrp cache", "show ip bgp summary")
            sas = _ssh(n["mgmt_ip"], "sudo swanctl --list-sas; cat /proc/loadavg", VYOS_USER, VYOS_PASS, timeout=45)
        except Exception as e:                                    # noqa: BLE001
            out["error"] = e.__class__.__name__
            return out
        hub_tun = {m["nodes"][h]["tunnel_ip"]: h for h in m["hubs"]}
        by_tun = {x["tunnel_ip"]: name for name, x in m["nodes"].items() if x.get("tunnel_ip")}
        # Iface  Type  Protocol  NBMA  Claimed-NBMA  Flags  Identity
        cache = re.findall(r"^\S+\s+(nhs|dynamic|static|local|cached)\s+(\d+\.\d+\.\d+\.\d+)(?:/\d+)?\s+(\S+)\s+\S+\s*(\S*)", text, re.M)
        out["nhrp"] = [{"type": t, "tunnel": ip, "nbma": nb, "flags": fl, "peer": by_tun.get(ip, ip)} for t, ip, nb, fl in cache]
        out["nhs_up"] = sorted({hub_tun[ip] for t, ip, nb, fl in cache if t == "nhs" and ip in hub_tun and nb not in ("-", "0.0.0.0")})
        out["shortcuts"] = sorted({by_tun[ip] for t, ip, nb, fl in cache
                                   if t == "dynamic" and ip in by_tun and ip not in hub_tun and ip != n["tunnel_ip"]})
        # strongSwan: every installed CHILD_SA is a protected GRE flow to one peer
        peers = re.findall(r"ESTABLISHED.*?\n\s+local.*?\n\s+remote\s+'?[^']*'?\s*@\s*(\d+\.\d+\.\d+\.\d+)", sas)
        out["sa_peers"] = sorted(set(peers))
        out["sa"] = len(re.findall(r"INSTALLED", sas))
        load = re.search(r"^(\d+\.\d+) (\d+\.\d+)", sas, re.M)
        out["cpu_5s"] = out["cpu_1m"] = None
        out["load_1m"] = float(load[2]) if load else None
        overlay = ipaddress.ip_network(m["service"]["overlay"])
        sess = [{"peer": p, "as": int(a), "state": st} for p, a, st in FRR_ROW.findall(text)]
        ov = [x for x in sess if ipaddress.ip_address(x["peer"]) in overlay]
        un = [x for x in sess if ipaddress.ip_address(x["peer"]) not in overlay]
        out["overlay_up"] = sum(1 for x in ov if x["state"].isdigit())
        out["overlay"] = len(ov)
        out["underlay_up"] = sum(1 for x in un if x["state"].isdigit())
        out["underlay"] = len(un)
        return out

    def router_state(self, node, n, m):
        if n.get("platform") == "vyos":
            return self.vyos_router_state(node, n, m)
        out = {"name": node, "role": n["role"], "error": None, "tunnel_ip": n["tunnel_ip"], "nbma": n["nbma"]}
        try:
            r = ios(n["mgmt_ip"], "show dmvpn", "show crypto session brief", "show ip bgp summary",
                    "show processes cpu | include CPU utilization")
        except Exception as e:                                    # noqa: BLE001
            out["error"] = e.__class__.__name__
            return out
        hub_tun = {m["nodes"][h]["tunnel_ip"]: h for h in m["hubs"]}
        by_tun = {x["tunnel_ip"]: name for name, x in m["nodes"].items() if x.get("tunnel_ip")}
        rows = [{"nbma": x[0], "tunnel": x[1], "state": x[2], "uptime": x[3], "attr": x[4],
                 "peer": by_tun.get(x[1], x[1])} for x in DMVPN_ROW.findall(r["show dmvpn"])]
        out["nhrp"] = rows
        if n["role"] == "hub":
            out["registered"] = sorted(x["peer"] for x in rows if x["state"] == "UP" and "D" in x["attr"])
        else:
            out["nhs_up"] = sorted(hub_tun[x["tunnel"]] for x in rows
                                   if x["tunnel"] in hub_tun and x["state"] == "UP" and "S" in x["attr"])
            # a dynamic entry for another customer is a phase 3 shortcut: that pair talks directly
            out["shortcuts"] = sorted(x["peer"] for x in rows
                                      if x["tunnel"] not in hub_tun and x["state"] == "UP" and "D" in x["attr"])
        sa = re.findall(r"^(\d+\.\d+\.\d+\.\d+)\s+Tu0\s+.*\s(\S+)\s*$", r["show crypto session brief"], re.M)
        out["sa"] = sum(1 for _, st in sa if st == "UA")
        out["sa_peers"] = sorted(p for p, st in sa if st == "UA")
        cpu = re.search(r"five seconds: (\d+)%.*one minute: (\d+)%", r["show processes cpu | include CPU utilization"])
        out["cpu_5s"], out["cpu_1m"] = (int(cpu[1]), int(cpu[2])) if cpu else (None, None)
        overlay = ipaddress.ip_network(m["service"]["overlay"])
        sess = [{"peer": p, "as": int(a), "state": s} for p, a, s in BGP_ROW.findall(r["show ip bgp summary"])]
        ov = [s for s in sess if ipaddress.ip_address(s["peer"]) in overlay]
        un = [s for s in sess if ipaddress.ip_address(s["peer"]) not in overlay]
        out["overlay_up"] = sum(1 for s in ov if s["state"].isdigit())
        out["overlay"] = len(ov)
        out["underlay_up"] = sum(1 for s in un if s["state"].isdigit())
        out["underlay"] = len(un)
        return out

    def provider_state(self, node, n):
        out = {"name": node, "role": "provider", "error": None}
        try:
            text = _ssh(n["mgmt_ip"], "ip -br -4 addr show | grep -v '^lo'", VYOS_USER, VYOS_PASS)
            bgpsum = vtysh(n["mgmt_ip"], "show bgp ipv4 unicast summary")
        except Exception as e:                                    # noqa: BLE001
            out["error"] = e.__class__.__name__
            return out
        out["interfaces"] = [{"name": p[0], "state": p[1], "address": p[2] if len(p) > 2 else None}
                             for p in (l.split() for l in text.splitlines() if l.strip())]
        out["up"] = sum(1 for i in out["interfaces"] if i["state"] == "UP")
        sess = FRR_ROW.findall(bgpsum)
        out["customers_up"] = sum(1 for _, _, s in sess if s.isdigit())
        out["customers"] = len(sess)
        out["sessions"] = [{"peer": p, "state": s} for p, _, s in sess]
        return out

    def host_reachable(self, n):
        try:
            _ssh(n["mgmt_ip"], "true", HOST_USER, HOST_PASS, timeout=15)
            return True
        except Exception:                                         # noqa: BLE001
            return False

    # ---- the whole picture -------------------------------------------------------------------------------------
    def get(self, refresh=False, live=True):
        with self._lock:
            hit = self._cache.get(bool(live))
            if hit and not refresh and time.time() - hit["generated"] < self.ttl:
                return hit
        m = self.model()
        snap = {**m, "live": live, "generated": time.time()}
        if live:
            routers = m["hubs"] + m["customers"]
            provs = m["provider"]["nodes"]
            hosts = [h for h in m["nodes"] if m["nodes"][h]["role"] == "host"]
            with ThreadPoolExecutor(max_workers=12) as ex:
                states = dict(zip(routers, ex.map(lambda x: self.router_state(x, m["nodes"][x], m), routers)))
                prov = dict(zip(provs, ex.map(lambda x: self.provider_state(x, m["nodes"][x]), provs)))
                up = dict(zip(hosts, ex.map(lambda x: self.host_reachable(m["nodes"][x]), hosts)))
            snap.update(cloud=states, provider_state=prov, hosts_up=up, health=self._health(m, states, prov, up))
        with self._lock:
            self._cache[bool(live)] = snap
        return snap

    def _health(self, m, states, prov, hosts_up):
        hubs, custs = m["hubs"], m["customers"]
        problems = []
        for r in hubs + custs:
            s = states.get(r) or {}
            if s.get("error"):
                problems.append(f"{r}: unreachable ({s['error']})")
                continue
            if s.get("underlay_up", 0) < 1:
                problems.append(f"{r}: no eBGP session with the provider — its NBMA address is not being carried")
            if s["role"] == "hub":
                missing = sorted(set(custs) - set(s.get("registered") or []))
                if missing:
                    problems.append(f"{r}: no registration from {', '.join(missing)}")
                want = len(hubs) - 1 + len(custs)
            else:
                if len(s.get("nhs_up") or []) < len(hubs):
                    problems.append(f"{r}: registered with {len(s.get('nhs_up') or [])} of {len(hubs)} hubs")
                want = len(hubs)
            if s.get("overlay_up", 0) < want:
                problems.append(f"{r}: {s.get('overlay_up', 0)} of {want} overlay BGP sessions up")
            if s.get("sa", 0) < want:
                problems.append(f"{r}: {s.get('sa', 0)} of at least {want} IPsec sessions up")
        for p, s in prov.items():
            if s.get("error"):
                problems.append(f"{p}: unreachable ({s['error']})")
            elif s.get("customers_up", 0) < len(hubs) + len(custs):
                problems.append(f"{p}: {s.get('customers_up', 0)} of {len(hubs) + len(custs)} sites peering with it")
        down = [h for h, ok in hosts_up.items() if not ok]
        if down:
            problems.append("hosts not answering: " + ", ".join(sorted(down)))
        return {"ok": not problems, "problems": problems, "hubs": len(hubs), "customers": len(custs),
                "shortcuts": sum(len((states.get(c) or {}).get("shortcuts") or []) for c in custs) // 2}
