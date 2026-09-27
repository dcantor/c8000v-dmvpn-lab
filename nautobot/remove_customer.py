#!/usr/bin/env python3
"""Take a decommissioned customer (and the host behind it) out of Nautobot: its BGP peerings and routing instance,
its addresses, its WAN and LAN prefixes, its cables and the devices themselves. The other half of seed.py, which only
ever adds and reconciles.

Usage: NAUTOBOT_TOKEN=... remove_customer.py NAME [--host HOST] [--wan PREFIX]... [--lan PREFIX] [--domain-prefix c8d-]
(--wan once per provider link: a dual-homed customer has two)"""
import argparse
import os

import pynautobot

p = argparse.ArgumentParser()
p.add_argument("name")
p.add_argument("--host")
p.add_argument("--wan", action="append", default=[])
p.add_argument("--lan")
p.add_argument("--domain-prefix", default="c8d-")
p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"))
p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
a = p.parse_args()
nb = pynautobot.api(a.url, token=a.token)
bgp = nb.plugins.bgp
ns = nb.ipam.namespaces.get(name="Global")
done = []

for node in filter(None, (a.name, a.host)):
    d = nb.dcim.devices.get(name=a.domain_prefix + node)
    if d is None:
        done.append(f"{a.domain_prefix}{node}: not in Nautobot")
        continue
    ri = bgp.routing_instances.get(device=d.id)
    if ri is not None:
        for ep in list(bgp.peer_endpoints.filter(routing_instance=ri.id)):
            peering = bgp.peerings.get(id=ep.peering.id)
            if peering is not None:
                peering.delete()          # takes both endpoints with it
                done.append(f"peering {ep.description}")
        ri.delete()
        done.append(f"routing instance of {d.name}")
    for itf in nb.dcim.interfaces.filter(device=d.id):
        for x in nb.ipam.ip_address_to_interface.filter(interface=itf.id):
            ip = nb.ipam.ip_addresses.get(id=getattr(x.ip_address, "id", x.ip_address))
            if ip is not None:
                ip.delete()
                done.append(f"ip {ip.address}")
        if itf.cable:
            cable = nb.dcim.cables.get(id=itf.cable.id)
            if cable is not None:
                cable.delete()
                done.append(f"cable on {itf.name}")
    d.delete()
    done.append(f"device {d.name}")

for wan in a.wan:   # the provider's end of the /30 is on the provider's (now unwired) port
    import ipaddress
    net = ipaddress.ip_network(wan)
    for host in net.hosts():
        ip = nb.ipam.ip_addresses.get(address=f"{host}/{net.prefixlen}", namespace=ns.id)
        if ip is not None:
            ip.delete()
            done.append(f"ip {ip.address}")
for prefix in filter(None, (*a.wan, a.lan)):
    pf = nb.ipam.prefixes.get(prefix=prefix, namespace=ns.id)
    if pf is not None:
        pf.delete()
        done.append(f"prefix {prefix}")
print(f"removed {a.name}: " + "; ".join(done))
