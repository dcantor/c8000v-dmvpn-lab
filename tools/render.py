#!/usr/bin/env python3
"""Render every configuration in the lab from an inventory (the JSON `lab.sh inventory` prints). This is the only
place configuration is written. Two producers feed it — `lab.sh inventory` (tools/gen_configs.py) and, later,
Nautobot — and their output must agree byte for byte.

   render_all(inv) -> {relative path: text}
      nodes/<c8000v>/iosxe_config.txt   day-0: identity, Mgmt-vrf on Gi1, AAA, SSH, NETCONF/RESTCONF, license level
      nodes/<provider>/vyos_config.txt  the MPLS provider: its access links and one eBGP peer-group for every site
      nac/data/devices.nac.yaml         the C8000v model for Network-as-Code: addresses, filters, BGP, Tunnel0

Design, in one paragraph. Every hub and customer is an eBGP customer of the provider (AS 65000) on Gi2 and offers
it exactly one prefix — its own WAN /30, the NBMA address its tunnel is sourced from. The provider accepts the sites
through a listen range and applies as-override (every site is AS 65100). Over the tunnel the same BGP process runs
iBGP: the hubs are route reflectors accepting customers on a listen range, and the customers peer with all three
hubs. Route-maps keep the planes apart — nothing learned from the provider enters the overlay, and nothing from the
overlay reaches the provider — so a lost WAN session can never be papered over by a route through the tunnel."""
import ipaddress
import json
import sys

import yaml

API_KEY = "c8000v-dmvpn-lab"


def mask(prefix):
    return str(ipaddress.ip_network(prefix).netmask)


def addr(cidr):
    return cidr.split("/")[0]


def classful(prefix):
    """IOS stores `network x mask m` without the mask when m is the classful one — model it the way it is stored,
    or Terraform sees permanent drift."""
    n = ipaddress.ip_network(prefix)
    first = int(str(n.network_address).split(".")[0])
    natural = 8 if first < 128 else 16 if first < 192 else 24
    return n.prefixlen == natural


class Renderer:
    def __init__(self, inv):
        self.inv = inv
        self.svc = inv["service"]
        self.prov = inv["provider"]
        self.nodes = {n["name"]: n for n in inv["nodes"]}
        self.hubs = [self.nodes[h] for h in self.svc["hubs"]]
        self.spokes = [self.nodes[s] for s in self.svc["spokes"]]
        self.overlay = ipaddress.ip_network(self.svc["overlay"])

    # ---- C8000v day-0 ---------------------------------------------------------------------------------------------
    def day0(self, n):
        oob = ipaddress.ip_network(self.inv["oob"]["prefix"])
        return "\n".join([
            f"! {n['name']}: day-0, delivered on the config ISO and pushed over the serial console by lab.sh bootstrap",
            "! rendered by tools/render.py — everything after day-0 is Network-as-Code (nac/)",
            f"hostname {n['name']}",
            "!",
            "aaa new-model",
            "aaa authentication login default local",
            "aaa authorization exec default local",
            "username admin privilege 15 secret admin",
            "enable secret admin",
            "ip domain name lab.local",
            "!",
            "! crypto (IKEv2/IPsec) needs the network-advantage feature set - boot level requires a reload",
            "license boot level network-advantage addon dna-advantage",
            "no ip domain lookup",
            "!",
            "vrf definition Mgmt-vrf",
            " address-family ipv4",
            " exit-address-family",
            "!",
            "interface GigabitEthernet1",
            " description OOB management",
            " vrf forwarding Mgmt-vrf",
            f" ip address {n['mgmt_ip']} {oob.netmask}",
            " negotiation auto",
            " no shutdown",
            "!",
            f"ip route vrf Mgmt-vrf 0.0.0.0 0.0.0.0 {self.inv['oob']['gateway']}",
            "!",
            "ip ssh version 2",
            "!",
            "! NETCONF/RESTCONF for the Network-as-Code Terraform provider",
            "netconf-yang",
            "restconf",
            "ip http secure-server",
            "ip http authentication local",
            "!",
            "line con 0",
            " exec-timeout 0 0",
            " logging synchronous",
            "line vty 0 4",
            " transport input ssh",
            "!",
            "end",
        ]) + "\n"

    # ---- the provider (VyOS) ------------------------------------------------------------------------------------
    def mac(self, n, port):
        return f"{self.inv['mac_oui']}:{n['idx']:02x}:{port:02x}"

    def provider(self, n):
        oob = ipaddress.ip_network(self.inv["oob"]["prefix"])
        out = [f"# {n['name']}: the MPLS provider — day-0 pushed over the serial console by lab.sh bootstrap; rendered by tools/render.py",
               f"set system host-name {n['name']}",
               "set system login user vyos authentication plaintext-password vyos",
               "set system time-zone UTC",
               "set service ssh",
               "set service lldp interface all",
               f"set interfaces ethernet eth0 address {n['mgmt_ip']}/{oob.prefixlen}",
               "set interfaces ethernet eth0 description 'OOB management'",
               f"set interfaces ethernet eth0 hw-id {self.mac(n, 0)}",
               # no default route: management is the directly connected OOB /24
               "set service https api rest",
               f"set service https api keys id lab key {API_KEY}",
               f"set service https allow-client address {self.inv['oob']['gateway']}",
               "#", "# access links: one /30 per site, the provider is .1"]
        for p in n["ports"]:
            out.append(f"set interfaces ethernet {p['name']} hw-id {self.mac(n, p['num'])}")
            if p["ip"]:
                out += [f"set interfaces ethernet {p['name']} address {p['ip']}",
                        f"set interfaces ethernet {p['name']} description '{p['peer_role']}: {p['peer']} {p['peer_port']}'"]
            else:
                out += [f"set interfaces ethernet {p['name']} description 'unwired'",
                        f"set interfaces ethernet {p['name']} disable"]
        pg, pl, wan = "CE", "WAN-ADDRESSES", self.prov["wan_net"]
        out += ["#", f"# {n['name']}: its own AS, eBGP with every site through a listen range; it carries the sites' WAN "
                     "addresses and nothing else",
                f"set policy prefix-list {pl} description 'the access links, and nothing else'",
                f"set policy prefix-list {pl} rule 10 action permit",
                f"set policy prefix-list {pl} rule 10 prefix {wan}",
                f"set policy prefix-list {pl} rule 10 le 32",
                f"set protocols bgp system-as {self.prov['as']}",
                f"set protocols bgp parameters router-id {n['router_id']}",
                "set protocols bgp parameters log-neighbor-changes",
                f"set protocols bgp timers keepalive {self.svc['keepalive']}",
                f"set protocols bgp timers holdtime {self.svc['holdtime_bgp']}",
                f"set protocols bgp peer-group {pg} remote-as {self.svc['as']}",
                f"set protocols bgp peer-group {pg} description 'the customer sites (AS {self.svc['as']})'",
                # every site is in the same customer AS, so their routes have to be rewritten to reach each other
                f"set protocols bgp peer-group {pg} address-family ipv4-unicast as-override",
                f"set protocols bgp peer-group {pg} address-family ipv4-unicast prefix-list export {pl}",
                f"set protocols bgp peer-group {pg} address-family ipv4-unicast prefix-list import {pl}",
                "set protocols bgp listen limit 64",
                f"set protocols bgp listen range {wan} peer-group {pg}"]
        return "\n".join(out) + "\n"

    # ---- C8000v: Network-as-Code ----------------------------------------------------------------------------------
    def tunnel_cli(self, n):
        hub = n["role"] == "hub"
        lines = ["interface Tunnel0",
                 f" description DMVPN {'hub' if hub else 'customer'} (mGRE, phase 3)",
                 f" ip address {n['tunnel_ip']} {self.overlay.netmask}",
                 " no ip redirects",
                 f" ip mtu {self.svc['mtu']}",
                 f" ip tcp adjust-mss {self.svc['mss']}",
                 f" ip nhrp authentication {self.svc['nhrp_secret']}",
                 f" ip nhrp network-id {self.svc['network_id']}",
                 f" ip nhrp holdtime {self.svc['holdtime']}"]
        if hub:
            lines.append(" ip nhrp map multicast dynamic")
            # the other hubs are static entries, not registrations: hub-to-hub iBGP needs their NBMA, and no hub is
            # another hub's client
            for h in self.hubs:
                if h is not n:
                    lines += [f" ip nhrp map {h['tunnel_ip']} {h['nbma']}", f" ip nhrp map multicast {h['nbma']}"]
            lines.append(" ip nhrp redirect")
        else:
            lines += [f" ip nhrp nhs {h['tunnel_ip']} nbma {h['nbma']} multicast" for h in self.hubs]
            lines.append(" ip nhrp shortcut")
        lines += [" tunnel source GigabitEthernet2",
                  " tunnel mode gre multipoint",
                  f" tunnel key {self.svc['tunnel_key']}",
                  " tunnel protection ipsec profile DMVPN-IPSEC"]
        return "\n".join(lines) + "\n"

    def hub_bgp_cli(self, n):
        """What the nac-iosxe 0.1.0 model cannot say: a peer-group and a listen range. With it a hub accepts any
        customer that comes up inside the overlay, so adding a customer never touches a hub."""
        a, k, h = self.svc["as"], self.svc["keepalive"], self.svc["holdtime_bgp"]
        return "\n".join([
            f"router bgp {a}",
            " neighbor CUSTOMERS peer-group",
            f" neighbor CUSTOMERS remote-as {a}",
            " neighbor CUSTOMERS description DMVPN customers (dynamic, listen range)",
            f" neighbor CUSTOMERS timers {k} {h}",
            " bgp listen limit 64",
            f" bgp listen range {self.svc['overlay']} peer-group CUSTOMERS",
            " address-family ipv4",
            "  neighbor CUSTOMERS activate",
            "  neighbor CUSTOMERS route-reflector-client",
            "  neighbor CUSTOMERS route-map OVERLAY in",
            "  neighbor CUSTOMERS route-map OVERLAY out",
            " exit-address-family",
        ]) + "\n"

    def device(self, n):
        hub = n["role"] == "hub"
        wan = n["wan"]
        ethernets = [{"type": "GigabitEthernet", "id": "2", "description": f"WAN: {wan['peer']} {wan['peer_port']} (provider AS {self.prov['as']})",
                      "shutdown": False,
                      "ipv4": {"address": addr(wan["ip"]), "address_mask": mask(wan["prefix"])}}]
        loopbacks = [{"id": 0, "description": "router-id",
                      "ipv4": {"address": n["router_id"], "address_mask": "255.255.255.255"}}]
        lan = ipaddress.ip_network(n["lan"])
        lan_gw = str(lan.network_address + 1)
        if hub:
            loopbacks.append({"id": 10, "description": "hub LAN",
                              "ipv4": {"address": lan_gw, "address_mask": str(lan.netmask)}})
        else:
            lp = n["lan_port"]
            ethernets.append({"type": "GigabitEthernet", "id": str(lp["num"]), "description": f"site LAN: {lp['peer']}",
                              "shutdown": False,
                              "ipv4": {"address": addr(lp["ip"]), "address_mask": mask(lp["prefix"])}})

        k, h = self.svc["keepalive"], self.svc["holdtime_bgp"]
        provider_peer = {"ip": wan["peer_ip"], "remote_as": self.prov["as"],
                         "description": f"provider {wan['peer']}", "timers_keepalive": k, "timers_holdtime": h}
        overlay_peers = [x for x in self.hubs if x is not n]
        neighbors = [provider_peer] + [{"ip": x["tunnel_ip"], "remote_as": self.svc["as"],
                                        "description": f"DMVPN hub {x['name']}", "timers_keepalive": k,
                                        "timers_holdtime": h} for x in overlay_peers]
        af_neighbors = [{"ip": wan["peer_ip"], "activate": True,
                         "route_maps": [{"direction": "in", "name": "WAN-IN"}, {"direction": "out", "name": "WAN-OUT"}]}]
        af_neighbors += [{"ip": x["tunnel_ip"], "activate": True,
                          "route_maps": [{"direction": "in", "name": "OVERLAY"}, {"direction": "out", "name": "OVERLAY"}]}
                         for x in overlay_peers]
        networks = [{"network": n["router_id"], "mask": "255.255.255.255"},
                    {"network": str(lan.network_address)} if classful(n["lan"]) else
                    {"network": str(lan.network_address), "mask": str(lan.netmask)},
                    {"network": str(ipaddress.ip_network(wan["prefix"]).network_address), "mask": mask(wan["prefix"])}]

        templates = [f"tunnel0_{n['name']}"] + ([f"bgp_hub_{n['name']}"] if hub else [])
        return {
            "name": n["name"], "host": n["mgmt_ip"], "protocol": "restconf",
            "device_groups": ["DMVPN", "DMVPN_HUB" if hub else "DMVPN_CUSTOMER"],
            "templates": templates,
            "variables": {"router_id": n["router_id"], "nbma": n["nbma"], "tunnel_ip": n["tunnel_ip"],
                          "lan": n["lan"], "region": n["region"],
                          "dmvpn_psk": self.svc["psk"]},
            "configuration": {
                "system": {"hostname": n["name"]},
                "interfaces": {"ethernets": ethernets, "loopbacks": loopbacks},
                "prefix_lists": [
                    {"name": "WAN-OUT", "description": "offer the provider our own access link and nothing else",
                     "seqs": [{"seq": 10, "action": "permit", "prefix": wan["prefix"]}]},
                    {"name": "WAN-IN", "description": "accept only the provider's access-link range",
                     "seqs": [{"seq": 10, "action": "permit", "prefix": self.prov["wan_net"], "less_equal": 32}]},
                    {"name": "OVERLAY-ROUTES", "description": "site LANs and router-ids: all the overlay carries",
                     "seqs": [{"seq": 10, "action": "permit", "prefix": "192.168.0.0/16", "greater_equal": 24, "less_equal": 24},
                              {"seq": 20, "action": "permit", "prefix": f"{n['router_id'].rsplit('.', 1)[0]}.0/24",
                               "greater_equal": 32}]},   # IOS stores `ge 32 le 32` as `ge 32`
                ],
                "route_maps": [
                    {"name": "WAN-OUT", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN-OUT"]}}]},
                    {"name": "WAN-IN", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN-IN"]}}]},
                    {"name": "OVERLAY", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["OVERLAY-ROUTES"]}}]},
                ],
                "routing": {"bgp": {
                    "as_number": self.svc["as"], "router_id": n["router_id"], "log_neighbor_changes": True,
                    "neighbors": neighbors,
                    "address_family": {"ipv4_unicast": {"neighbors": af_neighbors, "networks": networks}},
                }},
            },
        }

    def nac_devices(self):
        c8k = self.hubs + self.spokes
        templates = [{"name": f"tunnel0_{n['name']}", "type": "cli", "content": self.tunnel_cli(n)} for n in c8k]
        templates += [{"name": f"bgp_hub_{n['name']}", "type": "cli", "content": self.hub_bgp_cli(n)} for n in self.hubs]
        doc = {"iosxe": {"templates": templates, "devices": [self.device(n) for n in c8k]}}
        head = ("---\n# GENERATED by tools/render.py from `lab.sh inventory` — do not edit by hand.\n"
                "# Per-router facts only; what every router shares is in device_groups.nac.yaml and global.nac.yaml.\n")
        return head + yaml.safe_dump(doc, sort_keys=False, width=120, default_flow_style=False)


def render_all(inv):
    r = Renderer(inv)
    out = {}
    for n in inv["nodes"]:
        if n["role"] in ("hub", "spoke"):
            out[f"nodes/{n['name']}/iosxe_config.txt"] = r.day0(n)
        elif n["role"] == "provider":
            out[f"nodes/{n['name']}/vyos_config.txt"] = r.provider(n)
    out["nac/data/devices.nac.yaml"] = r.nac_devices()
    return out


if __name__ == "__main__":
    inv = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
    for path, text in render_all(inv).items():
        print(f"===== {path} =====\n{text}")
