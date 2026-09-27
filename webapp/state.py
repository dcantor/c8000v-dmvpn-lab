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
        return {"service": inv["service"], "provider": inv["provider"], "provider2": inv.get("provider2"), "oob": inv["oob"], "hubs": f["hubs"],
                "customers": f["customers"], "nodes": {n["name"]: n for n in inv["nodes"]},
                "applications": inv.get("applications") or []}

    # ---- per router --------------------------------------------------------------------------------------------
    @staticmethod
    def _maps(m):
        """hub tunnel address -> hub, any tunnel address -> router, and address -> cloud (1 or 2)."""
        hub_tun, by_tun, cloud = {}, {}, {}
        for name, x in m["nodes"].items():
            for key, c in (("tunnel_ip", 1), ("tunnel2_ip", 2)):
                if x.get(key):
                    by_tun[x[key]] = name
                    cloud[x[key]] = c
                    if x["role"] == "hub":
                        hub_tun[x[key]] = name
        return hub_tun, by_tun, cloud

    @staticmethod
    def _overlays(m):
        nets = [ipaddress.ip_network(m["service"]["overlay"])]
        if m["service"].get("cloud2"):
            nets.append(ipaddress.ip_network(m["service"]["cloud2"]["overlay"]))
        return nets

    def vyos_router_state(self, node, n, m):
        """A VyOS customer, from FRR (nhrpd, bgpd) and strongSwan: the same fields the IOS parser produces."""
        out = {"name": node, "role": n["role"], "platform": "vyos", "error": None, "tunnel_ip": n["tunnel_ip"], "nbma": n["nbma"]}
        try:
            text = vtysh(n["mgmt_ip"], "show ip nhrp cache", "show ip bgp summary")
            sas = _ssh(n["mgmt_ip"], "sudo swanctl --list-sas; cat /proc/loadavg", VYOS_USER, VYOS_PASS, timeout=45)
        except Exception as e:                                    # noqa: BLE001
            out["error"] = e.__class__.__name__
            return out
        hub_tun, by_tun, cloud = self._maps(m)
        # Iface  Type  Protocol  NBMA  Claimed-NBMA  Flags  Identity
        cache = re.findall(r"^\S+\s+(nhs|dynamic|static|local|cached)\s+(\d+\.\d+\.\d+\.\d+)(?:/\d+)?\s+(\S+)\s+\S+\s*(\S*)", text, re.M)
        out["nhrp"] = [{"type": t, "tunnel": ip, "nbma": nb, "flags": fl, "peer": by_tun.get(ip, ip), "cloud": cloud.get(ip, 1)}
                       for t, ip, nb, fl in cache]
        up = [(ip, cloud.get(ip)) for t, ip, nb, fl in cache if t == "nhs" and ip in hub_tun and nb not in ("-", "0.0.0.0")]
        out["nhs_up"] = sorted({hub_tun[ip] for ip, c in up if c == 1})
        out["nhs2_up"] = sorted({hub_tun[ip] for ip, c in up if c == 2})
        own = {n["tunnel_ip"], n.get("tunnel2_ip")}
        out["shortcuts"] = sorted({by_tun[ip] for t, ip, nb, fl in cache
                                   if t == "dynamic" and ip in by_tun and ip not in hub_tun and ip not in own})
        # strongSwan: every installed CHILD_SA is a protected GRE flow to one peer
        peers = re.findall(r"ESTABLISHED.*?\n\s+local.*?\n\s+remote\s+'?[^']*'?\s*@\s*(\d+\.\d+\.\d+\.\d+)", sas)
        out["sa_peers"] = sorted(set(peers))
        out["sa"] = len(re.findall(r"INSTALLED", sas))
        load = re.search(r"^(\d+\.\d+) (\d+\.\d+)", sas, re.M)
        out["cpu_5s"] = out["cpu_1m"] = None
        out["load_1m"] = float(load[2]) if load else None
        overlays = self._overlays(m)
        inov = lambda p: any(ipaddress.ip_address(p) in o for o in overlays)   # noqa: E731
        sess = [{"peer": p, "as": int(a), "state": st} for p, a, st in FRR_ROW.findall(text)]
        ov = [x for x in sess if inov(x["peer"])]
        un = [x for x in sess if not inov(x["peer"])]
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
        hub_tun, by_tun, cloud = self._maps(m)
        rows = [{"nbma": x[0], "tunnel": x[1], "state": x[2], "uptime": x[3], "attr": x[4],
                 "peer": by_tun.get(x[1], x[1]), "cloud": cloud.get(x[1], 1)} for x in DMVPN_ROW.findall(r["show dmvpn"])]
        out["nhrp"] = rows
        if n["role"] == "hub":
            out["registered"] = sorted({x["peer"] for x in rows if x["state"] == "UP" and "D" in x["attr"] and x["cloud"] == 1})
            out["registered2"] = sorted({x["peer"] for x in rows if x["state"] == "UP" and "D" in x["attr"] and x["cloud"] == 2})
        else:
            nhs = [(hub_tun[x["tunnel"]], x["cloud"]) for x in rows
                   if x["tunnel"] in hub_tun and x["state"] == "UP" and "S" in x["attr"]]
            out["nhs_up"] = sorted({h for h, c in nhs if c == 1})
            out["nhs2_up"] = sorted({h for h, c in nhs if c == 2})
            # a dynamic entry for another customer is a phase 3 shortcut: that pair talks directly
            out["shortcuts"] = sorted({x["peer"] for x in rows
                                       if x["tunnel"] not in hub_tun and x["state"] == "UP" and "D" in x["attr"]})
        sa = re.findall(r"^(\d+\.\d+\.\d+\.\d+)\s+Tu\d+\s+.*\s(\S+)\s*$", r["show crypto session brief"], re.M)
        out["sa"] = sum(1 for _, st in sa if st == "UA")
        out["sa_peers"] = sorted(p for p, st in sa if st == "UA")
        cpu = re.search(r"five seconds: (\d+)%.*one minute: (\d+)%", r["show processes cpu | include CPU utilization"])
        out["cpu_5s"], out["cpu_1m"] = (int(cpu[1]), int(cpu[2])) if cpu else (None, None)
        overlays = self._overlays(m)
        inov = lambda p: any(ipaddress.ip_address(p) in o for o in overlays)   # noqa: E731
        sess = [{"peer": p, "as": int(a), "state": s} for p, a, s in BGP_ROW.findall(r["show ip bgp summary"])]
        ov = [s for s in sess if inov(s["peer"])]
        un = [s for s in sess if not inov(s["peer"])]
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
            provs = m["provider"]["nodes"] + ((m.get("provider2") or {}).get("nodes") or [])
            hosts = [h for h in m["nodes"] if m["nodes"][h]["role"] == "host"]
            with ThreadPoolExecutor(max_workers=12) as ex:
                states = dict(zip(routers, ex.map(lambda x: self.router_state(x, m["nodes"][x], m), routers)))
                prov = dict(zip(provs, ex.map(lambda x: self.provider_state(x, m["nodes"][x]), provs)))
                up = dict(zip(hosts, ex.map(lambda x: self.host_reachable(m["nodes"][x]), hosts)))
            snap.update(cloud=states, provider_state=prov, hosts_up=up, health=self._health(m, states, prov, up))
        with self._lock:
            self._cache[bool(live)] = snap
        return snap

    @staticmethod
    def expectations(m):
        """What each router and provider should show: sessions and registrations, over one cloud or both."""
        hubs, custs = m["hubs"], m["customers"]
        duals = [c for c in custs if m["nodes"][c].get("wan2")]
        c2 = bool(m.get("provider2"))
        exp = {}
        for r in hubs + custs:
            dual = bool(m["nodes"][r].get("wan2"))
            if m["nodes"][r]["role"] == "hub":
                want = len(hubs) - 1 + len(custs) + ((len(hubs) - 1 + len(duals)) if dual else 0)
                exp[r] = {"overlay": want, "sa": want, "underlay": 1 + dual, "registered": len(custs),
                          "registered2": len(duals) if dual else 0}
            else:
                want = len(hubs) * (1 + dual)
                exp[r] = {"overlay": want, "sa": want, "underlay": 1 + dual, "nhs": len(hubs), "nhs2": len(hubs) if dual else 0}
        for p in m["provider"]["nodes"]:
            exp[p] = {"customers": len(hubs) + len(custs)}
        if c2:
            for p in m["provider2"]["nodes"]:
                exp[p] = {"customers": sum(1 for r in hubs + custs if m["nodes"][r].get("wan2"))}
        return exp

    def _health(self, m, states, prov, hosts_up):
        hubs, custs = m["hubs"], m["customers"]
        duals = [c for c in custs if m["nodes"][c].get("wan2")]
        exp = self.expectations(m)
        problems = []
        for r in hubs + custs:
            s = states.get(r) or {}
            e = exp[r]
            s["expect"] = e
            if s.get("error"):
                problems.append(f"{r}: unreachable ({s['error']})")
                continue
            if s.get("underlay_up", 0) < 1:
                problems.append(f"{r}: no eBGP session with the provider — its NBMA address is not being carried")
            elif s.get("underlay_up", 0) < e["underlay"]:
                problems.append(f"{r}: {s.get('underlay_up', 0)} of {e['underlay']} provider sessions up")
            if s["role"] == "hub":
                missing = sorted(set(custs) - set(s.get("registered") or []))
                if missing:
                    problems.append(f"{r}: no registration from {', '.join(missing)}")
                if e["registered2"]:
                    missing2 = sorted(set(duals) - set(s.get("registered2") or []))
                    if missing2:
                        problems.append(f"{r}: no cloud-2 registration from {', '.join(missing2)}")
            else:
                if len(s.get("nhs_up") or []) < len(hubs):
                    problems.append(f"{r}: registered with {len(s.get('nhs_up') or [])} of {len(hubs)} hubs")
                if e["nhs2"] and len(s.get("nhs2_up") or []) < e["nhs2"]:
                    problems.append(f"{r}: registered with {len(s.get('nhs2_up') or [])} of {e['nhs2']} hubs over cloud 2")
            want = e["overlay"]
            if s.get("overlay_up", 0) < want:
                problems.append(f"{r}: {s.get('overlay_up', 0)} of {want} overlay BGP sessions up")
            if s.get("sa", 0) < want:
                problems.append(f"{r}: {s.get('sa', 0)} of at least {want} IPsec sessions up")
        for p, s in prov.items():
            want = exp[p]["customers"]
            s["expect"] = exp[p]
            if s.get("error"):
                problems.append(f"{p}: unreachable ({s['error']})")
            elif s.get("customers_up", 0) < want:
                problems.append(f"{p}: {s.get('customers_up', 0)} of {want} sites peering with it")
        down = [h for h, ok in hosts_up.items() if not ok]
        if down:
            problems.append("hosts not answering: " + ", ".join(sorted(down)))
        return {"ok": not problems, "problems": problems, "hubs": len(hubs), "customers": len(custs),
                "shortcuts": sum(len((states.get(c) or {}).get("shortcuts") or []) for c in custs) // 2}
