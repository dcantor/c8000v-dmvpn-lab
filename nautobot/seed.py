#!/usr/bin/env python3
"""Seed the shared Nautobot with c8000v-dmvpn-lab (idempotent). Source: `lab.sh inventory` (i.e. lab.conf).

What is modelled
  locations      site "c8000v-dmvpn-lab" (type Site) with a Region per hub (c8d-east / -central / -west); each
                 customer sits in its hub's region, the provider at the site
  devices        named by their libvirt domain (c8d-<node>) — Nautobot names are global, and vyos-dmvpn already has
                 a hub-east and an mpls. C8000v hubs / customers (roles dmvpn-hub / dmvpn-spoke, platform cisco_xe),
                 the VyOS provider (role wan-provider, platform vyos), the Alpine hosts (role host)
  interfaces     every port with its lab MAC; Gi1 / eth0 = OOB (primary IPv4); Loopback0 (router-id), Loopback10
                 (a hub's LAN), Tunnel0 (the cloud) as virtual interfaces; cables from LINKS
  IPAM           prefixes with roles oob-management / wan-p2p / dmvpn-overlay / site-lan / router-id
  BGP            nautobot-bgp-models: AS 65100 and AS 65000, a routing instance per router, and a peering per session
                 — hub to hub, hub (rr) to customer (rr-client), every site (customer) to the provider (provider)
  config context c8000v-dmvpn-lab: the DMVPN service, the provider, the OOB network — what the renderer reads
  GraphQL        saved query c8000v-dmvpn-lab-model — what nautobot/render.py reads to rebuild the inventory

The pre-shared key and the NHRP secret stay in lab.conf; they are not modelled.

Usage: NAUTOBOT_TOKEN=... seed.py [--url http://10.0.0.10:8080] [--check]
  --check   dry run: every write is recorded instead of sent; exit 1 if Nautobot is not in sync with lab.conf"""
import argparse
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

import pynautobot
import requests

LAB = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"))
p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
p.add_argument("--check", action="store_true", help="dry run: report what would change, exit 1 if anything")
a = p.parse_args()

inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SVC, PROV, OOB = inv["service"], inv["provider"], inv["oob"]
N = {n["name"]: n for n in inv["nodes"]}
SITE = inv["lab"]
QUERY_NAME = f"{SITE}-model"
MAC_OUI = inv["mac_oui"]
nb = pynautobot.api(a.url, token=a.token)
H = {"Authorization": f"Token {a.token}", "Accept": "application/json"}
created = []

if a.check:   # reads stay live; creates / deletes / raw writes become notes in `created`
    from pynautobot.core.endpoint import Endpoint
    from pynautobot.core.response import Record

    class DryRecord:
        def __init__(self, ep, **fields):
            self.__dict__.update(fields)
            self.id = f"dry-{ep.rsplit('/', 1)[-1]}"
            self.endpoint = ep

        def __getattr__(self, k):
            return None

        def update(self, data):
            return True

        def delete(self):
            created.append(f"[dry] delete {self.endpoint} {self.id}")
            return True

    def _dry_create(self, *args, api_version=None, **kw):
        fields = {**(args[0] if args and isinstance(args[0], dict) else {}), **kw}
        created.append(f"[dry] create {self.url.replace(a.url, '')}: {fields}")
        return DryRecord(self.url, **fields)

    def _dry_delete(self):
        created.append(f"[dry] delete {self.endpoint.url.replace(a.url, '')} "
                       f"{getattr(self, 'name', None) or getattr(self, 'prefix', None) or getattr(self, 'address', None) or self.id}")
        return True

    Endpoint.create, Record.update, Record.delete = _dry_create, (lambda self, data: True), _dry_delete
    # a read filtered on something only pretended into existence finds nothing, as it would after a real create
    _real_get, _real_filter = Endpoint.get, Endpoint.filter
    _dry = lambda kw: any(str(v).startswith("dry-") for v in kw.values())   # noqa: E731
    Endpoint.get = lambda self, *args, **kw: None if _dry(kw) else _real_get(self, *args, **kw)
    Endpoint.filter = lambda self, *args, **kw: [] if _dry(kw) else _real_filter(self, *args, **kw)
    _real_post = requests.post

    class _DryResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def _dry_write(method):
        def f(url, **kw):
            if method == "post" and url.endswith("/graphql/"):
                return _real_post(url, **kw)   # a read
            created.append(f"[dry] {method.upper()} {url.replace(a.url, '')} {kw.get('json', '')}")
            return _DryResponse()
        return f

    requests.post, requests.patch, requests.delete = _dry_write("post"), _dry_write("patch"), _dry_write("delete")


# ---- helpers (the same idioms as the other labs' seeds) ----------------------------------------------------------
def get_or_create(ep, lookup, **d):
    o = ep.get(**lookup)
    if o is None:
        o = ep.create(**lookup, **d)
        created.append(f"{ep.name}:{list(lookup.values())[0]}")
    return o


def current(v):
    if hasattr(v, "id"):
        return str(v.id)
    if hasattr(v, "value") and hasattr(v, "label"):
        return v.value
    if hasattr(v, "serialize"):
        return {k: current(getattr(v, k)) for k in v.serialize()}
    return v


def ensure(obj, **fields):
    ch = {}
    for k, v in fields.items():
        c = current(getattr(obj, k, None))
        same = c == v if isinstance(v, (dict, list)) else (
            str(c).lower() == str(v).lower() if k == "mac_address" else str(c) == str(v))
        if not same:
            ch[k] = v
    if ch:
        obj.update(ch)
        created.append(f"updated {getattr(obj, 'name', None) or getattr(obj, 'prefix', None) or getattr(obj, 'address', obj)}: {', '.join(ch)}")
    return obj


def gql(query):
    r = requests.post(f"{a.url}/api/graphql/", json={"query": query}, headers=H, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        sys.exit(f"GraphQL: {body['errors']}")
    return body["data"]


def patch(path, **fields):
    r = requests.patch(f"{a.url}/api/{path}/", json=fields, headers={**H, "Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()


def with_ct(role, ct):
    if ct not in role.content_types:
        role.update({"content_types": list(role.content_types) + [ct]})
    return role


active = nb.extras.statuses.get(name="Active")
connected = nb.extras.statuses.get(name="Connected")
for ct in ("nautobot_bgp_models.autonomoussystem", "nautobot_bgp_models.bgproutinginstance", "nautobot_bgp_models.peering"):
    if ct not in active.content_types:
        active.update({"content_types": list(active.content_types) + [ct]})
ns = nb.ipam.namespaces.get(name="Global")
dev_name = lambda n: N[n]["domain"]   # noqa: E731  (node -> Nautobot device name)

# ---- locations ---------------------------------------------------------------------------------------------------
lt_site = nb.dcim.location_types.get(name="Site")
lt_region = nb.dcim.location_types.get(name="Region")
site = get_or_create(nb.dcim.locations, {"name": SITE}, location_type=lt_site.id, status=active.id)
ensure(site, description="Catalyst 8000v DMVPN phase 3: three hubs, three customers, a VyOS MPLS provider "
                         "(/home/dcantor/c8000v-dmvpn-lab)")
regions = {}
for r in sorted({n["region"] for n in inv["nodes"] if n.get("region")}):
    regions[r] = get_or_create(nb.dcim.locations, {"name": f"c8d-{r.lower()}"}, location_type=lt_region.id,
                               parent=site.id, status=active.id)
    ensure(regions[r], parent=site.id, description=f"{r}: hub-{r.lower()} and the customer nearest to it")


def loc_of(n):
    if n["role"] == "host":
        n = N[n["router"]]
    return regions.get(n.get("region")) or site


# ---- roles, platforms, device types -------------------------------------------------------------------------------
drole = {"hub": get_or_create(nb.extras.roles, {"name": "dmvpn-hub"}, color="b71c1c", content_types=["dcim.device"]),
         "spoke": get_or_create(nb.extras.roles, {"name": "dmvpn-spoke"}, color="1565c0", content_types=["dcim.device"]),
         "provider": get_or_create(nb.extras.roles, {"name": "wan-provider"}, color="b45309", content_types=["dcim.device"]),
         "host": get_or_create(nb.extras.roles, {"name": "host"}, color="4caf50", content_types=["dcim.device"])}
prole = {}
for name, colour, desc in (("oob-management", "9e9e9e", "out-of-band management"),
                           ("wan-p2p", "607d8b", "point-to-point underlay links"),
                           ("dmvpn-overlay", "00838f", "addresses inside the DMVPN cloud"),
                           ("site-lan", "4caf50", "site LANs"),
                           ("router-id", "9c27b0", "BGP router-ids")):
    prole[name] = with_ct(get_or_create(nb.extras.roles, {"name": name}, color=colour, content_types=["ipam.prefix"],
                                        description=desc), "ipam.prefix")
brole = {n: with_ct(get_or_create(nb.extras.roles, {"name": n}, color=c, content_types=["nautobot_bgp_models.peerendpoint"]),
                    "nautobot_bgp_models.peerendpoint")
         for n, c in (("rr", "e65100"), ("rr-client", "b71c1c"), ("hub", "6a1b9a"), ("customer", "0e7490"),
                      ("provider", "b45309"))}
mf = {"cisco": get_or_create(nb.dcim.manufacturers, {"name": "Cisco"}),
      "vyos": get_or_create(nb.dcim.manufacturers, {"name": "VyOS"}),
      "alpine": get_or_create(nb.dcim.manufacturers, {"name": "Alpine Linux"})}
plat = {"c8k": nb.dcim.platforms.get(name="cisco_xe")
        or get_or_create(nb.dcim.platforms, {"name": "cisco_xe"}, manufacturer=mf["cisco"].id, network_driver="cisco_xe"),
        "vyos": nb.dcim.platforms.get(name="vyos")
        or get_or_create(nb.dcim.platforms, {"name": "vyos"}, manufacturer=mf["vyos"].id, network_driver="vyos"),
        "linux": nb.dcim.platforms.get(name="alpine") or nb.dcim.platforms.get(name="linux")
        or get_or_create(nb.dcim.platforms, {"name": "alpine"}, manufacturer=mf["alpine"].id, network_driver="linux")}
c8k_type = next(iter(nb.dcim.device_types.filter(model__ic="C8000V")), None) \
    or get_or_create(nb.dcim.device_types, {"model": "C8000V"}, manufacturer=mf["cisco"].id, u_height=0)
dt = {"c8k": c8k_type,
      "vyos": nb.dcim.device_types.get(model="VyOS", manufacturer=mf["vyos"].id)
      or get_or_create(nb.dcim.device_types, {"model": "VyOS"}, manufacturer=mf["vyos"].id, u_height=0),
      "linux": nb.dcim.device_types.get(model="Alpine VM", manufacturer=mf["alpine"].id)
      or get_or_create(nb.dcim.device_types, {"model": "Alpine VM"}, manufacturer=mf["alpine"].id, u_height=0)}
kind = lambda n: {"c8000v": "c8k", "vyos": "vyos", "alpine": "linux"}[n.get("platform") or "c8000v"]   # noqa: E731

# ---- prefixes -----------------------------------------------------------------------------------------------------
pq = {x["prefix"]: x for x in gql('{ prefixes { prefix locations { name } } }')["prefixes"]}   # locations are M2M


def ensure_prefix(prefix, role, description, location=None, **more):
    pf = nb.ipam.prefixes.get(prefix=prefix, namespace=ns.id)
    if pf is None:
        pf = nb.ipam.prefixes.create(prefix=prefix, namespace=ns.id, status=active.id, role=prole[role].id,
                                     description=description, **more)
        created.append(f"prefix:{prefix}")
        if location:
            patch(f"ipam/prefixes/{pf.id}", location=location)   # 'locations' is ignored; the singular alias works
    else:
        ensure(pf, role=prole[role].id, description=description, **more)
        want = {nb.dcim.locations.get(id=location).name} if location else set()
        if {x["name"] for x in pq.get(prefix, {}).get("locations", [])} != want:
            patch(f"ipam/prefixes/{pf.id}", location=location)
            created.append(f"prefix {prefix} location")
    return pf


ensure_prefix(OOB["prefix"], "oob-management", f"{SITE} OOB network (libvirt {OOB['network']}, host {OOB['gateway']})",
              location=site.id)
ensure_prefix(SVC["overlay"], "dmvpn-overlay",
              f"the DMVPN cloud: Tunnel0 on every router, network-id {SVC['network_id']}, tunnel key {SVC['tunnel_key']}",
              location=site.id)
ensure_prefix(PROV["wan_net"], "wan-p2p", f"the MPLS provider's access links (AS {PROV['as']}): one /30 per site, "
              "carried by eBGP", location=site.id, type="container")
rid_block = str(ipaddress.ip_network(f"{N[SVC['hubs'][0]]['router_id']}/24", strict=False))
ensure_prefix(rid_block, "router-id", "BGP router-ids (Loopback0) of the routers; the provider takes .254",
              location=site.id, type="container")
for n in inv["nodes"]:
    for port in n["ports"]:
        if port["ip"] and port == next(q for q in n["ports"] if q["prefix"] == port["prefix"]) and \
                (n["role"] == "provider" or port.get("peer_role") == "host"):
            role = "wan-p2p" if n["role"] == "provider" else "site-lan"
            owner = N[port["peer"]] if n["role"] == "provider" else n
            ensure_prefix(port["prefix"], role, f"{n['name']} {port['name']} <-> {port['peer']} {port['peer_port']}",
                          location=loc_of(owner).id)
    if n["role"] == "hub":
        ensure_prefix(n["lan"], "site-lan", f"{n['name']} LAN (Loopback10)", location=loc_of(n).id)

# ---- devices, interfaces, addresses --------------------------------------------------------------------------------
devs, ifs = {}, {}


def ensure_ip(address, description, iface=None):
    """A lab interface carries one address, so a renumbered one drops the old."""
    ip = nb.ipam.ip_addresses.get(address=address, namespace=ns.id)
    if ip is None:
        ip = nb.ipam.ip_addresses.create(address=address, namespace=ns.id, status=active.id, description=description)
        created.append(f"ip:{address}")
    else:
        ensure(ip, description=description)
    if iface is not None:
        for x in nb.ipam.ip_address_to_interface.filter(interface=iface.id):
            if str(getattr(x.ip_address, "id", x.ip_address)) != str(ip.id):
                x.delete()
                created.append(f"unassign old address from {iface.name}")
        if not nb.ipam.ip_address_to_interface.get(ip_address=ip.id, interface=iface.id):
            nb.ipam.ip_address_to_interface.create(ip_address=ip.id, interface=iface.id)
            created.append(f"assign {address} -> {iface.name}")
    return ip


COMMENTS = {
    "hub": "DMVPN hub (C8000v): NHRP server and iBGP route reflector; customers arrive on a BGP listen range",
    "spoke": "DMVPN customer: one mGRE tunnel registered with all three hubs, iBGP with all three",
    "provider": "The simulated MPLS provider (VyOS): its own AS, eBGP with every site on a listen range with "
                "as-override. It carries the sites' WAN addresses and nothing else",
    "host": "Alpine LAN host (iperf3 / tcpdump / mtr) behind a customer router",
}
for n in inv["nodes"]:
    k = kind(n)
    fields = dict(role=drole[n["role"]].id, device_type=dt[k].id, location=loc_of(n).id, platform=plat[k].id,
                  status=active.id, comments=COMMENTS[n["role"]])
    d = nb.dcim.devices.get(name=dev_name(n["name"]))
    if d is None:
        d = nb.dcim.devices.create(name=dev_name(n["name"]), **fields)
        created.append(f"device:{dev_name(n['name'])}")
    else:
        ensure(d, **fields)
    devs[n["name"]] = d

    def ensure_if(name, itype, description, mac=None, mgmt_only=False, _d=d, _n=n):
        i = nb.dcim.interfaces.get(device=_d.id, name=name)
        f = dict(type=itype, description=description, mgmt_only=mgmt_only, status=active.id,
                 **({"mac_address": mac} if mac else {}))
        if i is None:
            i = nb.dcim.interfaces.create(device=_d.id, name=name, **f)
            created.append(f"interface:{_n['name']} {name}")
        else:
            ensure(i, **f)
        ifs[(_n["name"], name)] = i
        return i

    oob_port = 1 if k == "c8k" else 0
    oob_name = "GigabitEthernet1" if k == "c8k" else "eth0"
    oob = ensure_if(oob_name, "1000base-t", "OOB management",
                    mac=f"{MAC_OUI}:{n['idx']:02x}:{oob_port:02x}", mgmt_only=True)
    ensure(d, primary_ip4=ensure_ip(f"{n['mgmt_ip']}/{ipaddress.ip_network(OOB['prefix']).prefixlen}",
                                    f"{n['name']} OOB", oob).id)
    for port in n["ports"]:
        desc = f"{port['peer_role']}: {port['peer']} {port['peer_port']}" if port["peer"] else "unwired"
        i = ensure_if(port["name"], "1000base-t", desc, mac=f"{MAC_OUI}:{n['idx']:02x}:{port['num']:02x}")
        if port["ip"]:
            ensure_ip(port["ip"], f"{n['name']} {port['name']}", i)
        else:
            for x in nb.ipam.ip_address_to_interface.filter(interface=i.id):
                x.delete()
                created.append(f"address unassigned from unwired {n['name']} {port['name']}")
    if k == "c8k":
        lo0 = ensure_if("Loopback0", "virtual", "router-id")
        ensure_ip(f"{n['router_id']}/32", f"{n['name']} router-id", lo0)
        tun = ensure_if("Tunnel0", "virtual", f"DMVPN {'hub' if n['role'] == 'hub' else 'customer'} (mGRE, phase 3), "
                                             f"sourced from GigabitEthernet2 {n['nbma']}")
        ensure_ip(f"{n['tunnel_ip']}/{ipaddress.ip_network(SVC['overlay']).prefixlen}", f"{n['name']} Tunnel0", tun)
        if n["role"] == "hub":
            lan = ipaddress.ip_network(n["lan"])
            lo10 = ensure_if("Loopback10", "virtual", "hub LAN")
            ensure_ip(f"{lan.network_address + 1}/{lan.prefixlen}", f"{n['name']} LAN", lo10)
    elif k == "vyos" and n["role"] == "spoke":       # a VyOS customer: dum0 carries the router-id, tun0 the cloud
        dum = ensure_if("dum0", "virtual", "router-id")
        ensure_ip(f"{n['router_id']}/32", f"{n['name']} router-id", dum)
        tun = ensure_if("tun0", "virtual", f"DMVPN customer (mGRE, phase 3), sourced from {n['wan']['name']} {n['nbma']}")
        ensure_ip(f"{n['tunnel_ip']}/{ipaddress.ip_network(SVC['overlay']).prefixlen}", f"{n['name']} tun0", tun)
    elif k == "vyos":
        ensure_ip(f"{n['router_id']}/32", f"{n['name']} router-id")


# ---- cables --------------------------------------------------------------------------------------------------------
def pending(*objs):
    """--check: an object that does not exist yet (or only as a dry record) has no cables or peerings to compare."""
    return any(o is None or str(getattr(o, "id", "")).startswith("dry-") for o in objs)


def ensure_cable(x, y, label):
    if pending(x, y):
        created.append(f"[dry] cable:{label}")
        return
    for itf in (x, y):
        cur = requests.get(f"{a.url}/api/dcim/interfaces/{itf.id}/", params={"depth": 1}, headers=H, timeout=30).json().get("cable")
        if not cur:
            continue
        c = requests.get(f"{a.url}/api/dcim/cables/{cur['id']}/", headers=H, timeout=30)
        if c.status_code == 404:
            continue
        c = c.json()
        if {c.get("termination_a_id"), c.get("termination_b_id")} != {x.id, y.id}:
            requests.delete(f"{a.url}/api/dcim/cables/{cur['id']}/", headers=H, timeout=30)
            created.append(f"removed cable on {itf.name} (dangling or re-wired)")
    x, y = nb.dcim.interfaces.get(x.id), nb.dcim.interfaces.get(y.id)
    if x.cable or y.cable:
        return
    nb.dcim.cables.create(termination_a_type="dcim.interface", termination_a_id=x.id,
                          termination_b_type="dcim.interface", termination_b_id=y.id, status=connected.id, label=label)
    created.append(f"cable:{label}")


for n in inv["nodes"]:
    for port in n["ports"]:
        if port["peer"] and port["ip"] and port["ip"].split("/")[0] == str(ipaddress.ip_network(port["prefix"]).network_address + 1):
            ensure_cable(ifs[(n["name"], port["name"])], ifs[(port["peer"], port["peer_port"])], port["prefix"])

# ---- BGP -------------------------------------------------------------------------------------------------------------
bgp = nb.plugins.bgp
AS = get_or_create(bgp.autonomous_systems, {"asn": SVC["as"]}, status=active.id, description="")
PAS = get_or_create(bgp.autonomous_systems, {"asn": PROV["as"]}, status=active.id, description="")
ensure(AS, description=f"{SITE}: the DMVPN sites — eBGP to the provider underneath, iBGP over the overlay")
ensure(PAS, description=f"{SITE}: the simulated MPLS provider")
ri = {}
for n in inv["nodes"]:
    if n["role"] == "host":
        continue
    rid = nb.ipam.ip_addresses.get(address=f"{n['router_id']}/32", namespace=ns.id)
    if pending(devs[n["name"]], rid):
        created.append(f"[dry] bgp-ri:{n['name']}")
        ri[n["name"]] = None
        continue
    inst = bgp.routing_instances.get(device=devs[n["name"]].id)
    own = PAS if n["role"] == "provider" else AS
    extra = {"log_neighbor_changes": True, "keepalive": SVC["keepalive"], "holdtime": SVC["holdtime_bgp"]}
    if n["role"] == "provider":
        extra.update({"as_override": True, "listen_range": PROV["wan_net"], "peer_group": "CE"})
    elif n["role"] == "hub":
        extra.update({"listen_range": SVC["overlay"], "peer_group": "CUSTOMERS"})
    desc = {"hub": "hub: a route reflector for the customers, which arrive on a listen range; eBGP customer of the provider",
            "spoke": "customer: an iBGP client of all three hubs, and an eBGP customer of the provider",
            "provider": "the MPLS provider: eBGP with every site on a listen range, as-override"}[n["role"]]
    if inst is None:
        inst = bgp.routing_instances.create(device=devs[n["name"]].id, autonomous_system=own.id, router_id=rid.id,
                                            status=active.id, description=desc, extra_attributes=extra)
        created.append(f"bgp-ri:{n['name']}")
    else:
        ensure(inst, autonomous_system=own.id, router_id=rid.id, description=desc, extra_attributes=extra)
    ri[n["name"]] = inst
    want = {"carries": PROV["wan_net"]} if n["role"] == "provider" else {"networks": [n["lan"], n["wan"]["prefix"]]}
    af = bgp.address_families.get(routing_instance=inst.id, afi_safi="ipv4_unicast")
    if af is None:
        bgp.address_families.create(routing_instance=inst.id, afi_safi="ipv4_unicast", extra_attributes=want)
        created.append(f"bgp-af:{n['name']}")
    elif current(af.extra_attributes) != want:
        af.update({"extra_attributes": want})
        created.append(f"bgp-af attrs:{n['name']}")


def ip_obj(address):
    return nb.ipam.ip_addresses.get(address=address, namespace=ns.id)


def ensure_peering(a_name, a_role, b_name, b_role, label, ip_a, ip_b, as_a, as_b):
    """One peering with two endpoints, matched on the A side by description."""
    a_desc, b_desc = f"to {b_name} ({label})", f"to {a_name} ({label})"
    if pending(ri[a_name], ri[b_name], ip_a, ip_b, as_a, as_b):
        created.append(f"[dry] bgp-peering:{a_name}-{b_name} {label}")
        return
    ep_a = next((e for e in bgp.peer_endpoints.filter(routing_instance=ri[a_name].id) if e.description == a_desc), None)
    if ep_a is None:
        peering = bgp.peerings.create(status=active.id)
        created.append(f"bgp-peering:{a_name}-{b_name} {label}")
        ep_a = bgp.peer_endpoints.create(peering=peering.id, routing_instance=ri[a_name].id, source_ip=ip_a.id,
                                         autonomous_system=as_a.id, role=brole[a_role].id, description=a_desc, enabled=True)
        ep_b = bgp.peer_endpoints.create(peering=peering.id, routing_instance=ri[b_name].id, source_ip=ip_b.id,
                                         autonomous_system=as_b.id, role=brole[b_role].id, description=b_desc, enabled=True)
    else:
        ensure(ep_a, source_ip=ip_a.id, autonomous_system=as_a.id, role=brole[a_role].id)
        ep_b = bgp.peer_endpoints.get(id=ep_a.peer.id)
        ensure(ep_b, source_ip=ip_b.id, autonomous_system=as_b.id, role=brole[b_role].id, description=b_desc)
    for ep, who in ((ep_a, a_name), (ep_b, b_name)):
        if bgp.peer_endpoint_address_families.get(peer_endpoint=ep.id, afi_safi="ipv4_unicast") is None:
            bgp.peer_endpoint_address_families.create(peer_endpoint=ep.id, afi_safi="ipv4_unicast", extra_attributes={})
            created.append(f"bgp-endpoint-af:{who} {label}")


plen = ipaddress.ip_network(SVC["overlay"]).prefixlen
tun_ip = lambda x: ip_obj(f"{N[x]['tunnel_ip']}/{plen}")   # noqa: E731
hubs, spokes, provider = SVC["hubs"], SVC["spokes"], PROV["nodes"][0]
for i, h in enumerate(hubs):
    for k in hubs[i + 1:]:
        ensure_peering(h, "hub", k, "hub", "overlay", tun_ip(h), tun_ip(k), AS, AS)
    for s in spokes:
        ensure_peering(h, "rr", s, "rr-client", "overlay", tun_ip(h), tun_ip(s), AS, AS)
for s in hubs + spokes:
    w = N[s]["wan"]
    ensure_peering(s, "customer", provider, "provider", "underlay", ip_obj(w["ip"]),
                   ip_obj(f"{w['peer_ip']}/{w['prefix'].split('/')[1]}"), AS, PAS)

# ---- customers: a tenant per company (customers.json), on the customer's router and its LAN host ------------------
# The lab's own tenant group, so no other lab's clean-up of its customers can touch these, and these none of theirs.
CUST_FIELDS = (("customer_industry", "Industry", "industry"), ("customer_address", "Address", "address"),
               ("customer_phone", "Phone", "phone"), ("customer_contact", "Contact", "contact"),
               ("customer_email", "Email", "email"), ("customer_account", "Account", "account"))
cf = {c.key: c for c in nb.extras.custom_fields.all()}
for key, label, _ in CUST_FIELDS:
    if key not in cf:
        cf[key] = nb.extras.custom_fields.create(key=key, label=label, type="text", content_types=["tenancy.tenant"],
                                                 grouping="Customer", description=f"the customer company's {label.lower()}")
        created.append(f"custom-field:{key}")
        if not a.check:   # Nautobot's per-content-type field cache ignores a brand-new field until it is saved again
            desc = cf[key].description
            cf[key].update({"description": desc + " "}); cf[key].update({"description": desc})
tg = get_or_create(nb.tenancy.tenant_groups, {"name": f"{SITE} customers"},
                   description=f"the companies behind the customer sites of {SITE} (fictional)")
tenant_of = {}
for c in SVC["spokes"]:
    cu = N[c].get("customer")
    if not cu:
        continue
    t = get_or_create(nb.tenancy.tenants, {"name": cu["company"]}, tenant_group=tg.id)
    ensure(t, tenant_group=tg.id, description=f"{cu['industry']} · customer site {c} ({N[c]['region']})")
    want = {key: cu.get(field, "") for key, _, field in CUST_FIELDS}
    have = {k: (getattr(t, "custom_fields", None) or {}).get(k) for k in want}
    if have != want:
        t.update({"custom_fields": want})
        created.append(f"tenant {cu['company']}: customer fields")
    tenant_of[c] = t
    if N[c].get("host"):
        tenant_of[N[c]["host"]] = t
for name, d in devs.items():
    t = tenant_of.get(name)
    ensure(d, tenant=t.id if t else None)
for t in nb.tenancy.tenants.filter(tenant_group=tg.id):          # a company whose site is gone
    if t.id not in {x.id for x in tenant_of.values()}:
        t.delete()
        created.append(f"removed tenant {t.name} (no customer site)")

# ---- applications: one load-balancer Virtual Server per application per hosting hub (applications.json) ----------
# The VIP is an address in the hub's LAN (role vip); the application's identity rides in custom fields; each customer's
# tenant is related to the Virtual Servers of the applications it subscribes to (customers.json: applications).
APPS = inv.get("applications") or []
APP_FIELDS = (("application_id", "Application ID"), ("application_name", "Application"),
              ("application_url", "URL"), ("application_description", "Description"))
for key, label in APP_FIELDS:
    if key not in cf:
        cf[key] = nb.extras.custom_fields.create(key=key, label=label, type="text", content_types=["load_balancers.virtualserver"],
                                                 grouping="Application", description=f"the hosted application's {label.lower()}")
        created.append(f"custom-field:{key}")
        if not a.check:
            desc = cf[key].description
            cf[key].update({"description": desc + " "}); cf[key].update({"description": desc})
lb = nb.load_balancers
rel = nb.extras.relationships.get(key="application_subscriptions")
if rel is None:
    rel = nb.extras.relationships.create(label="Application subscriptions", key="application_subscriptions", type="many-to-many",
                                         source_type="tenancy.tenant", destination_type="load_balancers.virtualserver",
                                         source_label="Subscribed applications", destination_label="Subscribers",
                                         description="which customer companies consume the application served by this VIP")
    created.append("relationship:application_subscriptions")
vip_role = with_ct(get_or_create(nb.extras.roles, {"name": "vip"}, color="00838f", content_types=["ipam.ipaddress"],
                                 description="a load-balancer virtual IP"), "ipam.ipaddress")
hub_ids = {str(devs[h].id) for h in SVC["hubs"]}
vs_of = {}                                                        # (app id, hub) -> Virtual Server
for app in APPS:
    for h in app["hubs"]:
        name = f"{app['id']} {app['name']} @ {h}"
        vip = ensure_ip(f"{app['vips'][h]}/32", f"VIP {app['id']} {app['name']} at {h}")
        ensure(vip, role=vip_role.id)
        fields = dict(vip=vip.id, port=app["port"], protocol=app["protocol"], load_balancer_type="layer7" if app["protocol"] in ("http", "https") else "layer4",
                      device=devs[h].id, enabled=True)
        cfs = {"application_id": app["id"], "application_name": app["name"], "application_url": app["url"],
               "application_description": app["description"]}
        vs = lb.virtual_servers.get(name=name)
        if vs is None:
            vs = lb.virtual_servers.create(name=name, **fields, custom_fields=cfs)
            created.append(f"virtual-server:{name}")
        else:
            ensure(vs, **fields)
            if {k: (vs.custom_fields or {}).get(k) for k in cfs} != cfs:
                vs.update({"custom_fields": cfs})
                created.append(f"virtual-server {name}: application fields")
        vs_of[(app["id"], h)] = vs
for vs in lb.virtual_servers.filter(device=list(hub_ids)) if hub_ids else []:   # an application or a hosting hub gone
    if (vs.custom_fields or {}).get("application_id") and vs.id not in {x.id for x in vs_of.values()}:
        vs.delete()
        created.append(f"removed virtual server {vs.name}")
# subscriptions: exactly the associations customers.json asks for, between this lab's tenants and its Virtual Servers
want = set()
for c in SVC["spokes"]:
    cu, t = N[c].get("customer") or {}, tenant_of.get(c)
    for aid in cu.get("applications") or []:
        for (app_id, h), vs in vs_of.items():
            if app_id == aid and t is not None:
                want.add((str(t.id), str(vs.id)))
ours_t = {str(t.id) for t in tenant_of.values()}
have = {}
for x in nb.extras.relationship_associations.filter(relationship=rel.key):
    src, dst = str(x.source_id), str(x.destination_id)
    if src in ours_t:
        have[(src, dst)] = x
for key in want - set(have):
    nb.extras.relationship_associations.create(relationship=rel.id, source_type="tenancy.tenant", source_id=key[0],
                                               destination_type="load_balancers.virtualserver", destination_id=key[1])
    created.append("subscription " + key[0][:8] + " -> " + key[1][:8])
for key in set(have) - want:
    have[key].delete()
    created.append("removed subscription " + key[0][:8] + " -> " + key[1][:8])

# ---- config context ----------------------------------------------------------------------------------------------
CTX = {"lab": SITE, "domain_name": "lab.local", "mac_oui": MAC_OUI, "oob": OOB,
       "domain_prefix": inv["nodes"][0]["domain"][: -len(inv["nodes"][0]["name"])],
       "dmvpn": {k: v for k, v in SVC.items() if k not in ("psk", "nhrp_secret")},
       "provider": PROV,
       "ipsec": {"key_exchange": "ikev2", "mode": "tunnel", "ikev2_profile": "DMVPN-IKEV2", "ipsec_profile": "DMVPN-IPSEC",
                 "proposal": {"encryption": "aes-cbc-256", "integrity": "sha256", "dh_group": 14}}}
ctx = nb.extras.config_contexts.get(name=SITE)
if ctx is None:
    nb.extras.config_contexts.create(name=SITE, description=f"{SITE} constants: the DMVPN service, the provider, OOB",
                                     locations=[site.id], data=CTX)
    created.append(f"config-context:{SITE}")
elif current(ctx.data) != CTX:
    patch(f"extras/config-contexts/{ctx.id}", data=CTX, locations=[site.id])
    created.append("config-context updated")

# ---- saved GraphQL query (what nautobot/render.py reads) -----------------------------------------------------------
QUERY = (LAB / "nautobot" / f"{QUERY_NAME}.graphql").read_text()
q = nb.extras.graphql_queries.get(name=QUERY_NAME)
if q is None:
    nb.extras.graphql_queries.create(name=QUERY_NAME, query=QUERY)
    created.append(f"graphql-query:{QUERY_NAME}")
elif q.query.rstrip() != QUERY.rstrip():
    q.update({"query": QUERY})
    created.append("graphql-query updated")

print(f"seed {'check' if a.check else 'complete'}: {len(created)} changes{' would be made' if a.check else ''}"
      + (":\n  " + "\n  ".join(created[:60]) + ("\n  ..." if len(created) > 60 else "") if created else " (already in sync)"))
if a.check and created:
    sys.exit(1)
