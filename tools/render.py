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
VM_PORT, VL_SYSLOG_PORT = 8428, 5514                 # on the NMS: VictoriaMetrics (InfluxDB write API), VictoriaLogs (syslog)
TELEGRAF_TOKEN = "c8dlab-telegraf".ljust(86, "_") + "=="   # VyOS insists on an InfluxDB-shaped token; VictoriaMetrics ignores it


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
        self.prov2 = inv.get("provider2")                     # the second provider, or None
        self.cloud2 = self.svc.get("cloud2")                  # the DMVPN cloud over it, or None

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

    def vyos_base(self, n, what, dc, ports_comment):
        """What every VyOS router here carries: identity, the OOB port, the API, the exporters, Telegraf, syslog, and
        every data port (addressed when wired, disabled and labelled when not)."""
        oob = ipaddress.ip_network(self.inv["oob"]["prefix"])
        out = [f"# {n['name']}: {what} — day-0 pushed over the serial console by lab.sh bootstrap; rendered by tools/render.py",
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
               f"set service https allow-client address {self.inv['oob']['gateway']}"]
        nms = self.inv["oob"]["nms"]
        out += ["#", "# monitoring: exporters on the OOB address, scraped from the NMS (the portal's /api/sd lists them);",
                "# Telegraf pushes host / FRR / service metrics to VictoriaMetrics, syslog goes to VictoriaLogs",
                f"set service monitoring prometheus node-exporter listen-address {n['mgmt_ip']}",
                f"set service monitoring prometheus frr-exporter listen-address {n['mgmt_ip']}",
                f"set service monitoring telegraf influxdb url http://{nms}",
                f"set service monitoring telegraf influxdb port {VM_PORT}",
                f"set service monitoring telegraf influxdb bucket {self.inv['lab']}",
                "set service monitoring telegraf influxdb authentication organization lab",
                f"set service monitoring telegraf influxdb authentication token {TELEGRAF_TOKEN}",
                f"set service monitoring telegraf global-tag lab value {self.inv['lab']}",
                f"set service monitoring telegraf global-tag role value {n['role']}",
                f"set service monitoring telegraf global-tag dc value {dc}",
                f"set system syslog remote {nms} port {VL_SYSLOG_PORT}",
                f"set system syslog remote {nms} protocol udp",
                f"set system syslog remote {nms} facility all level info",
                "#", f"# {ports_comment}"]
        for p in n["ports"]:
            out.append(f"set interfaces ethernet {p['name']} hw-id {self.mac(n, p['num'])}")
            if p["ip"]:
                out += [f"set interfaces ethernet {p['name']} address {p['ip']}",
                        f"set interfaces ethernet {p['name']} description '{p['peer_role']}: {p['peer']} {p['peer_port']}'"]
            else:
                out += [f"set interfaces ethernet {p['name']} description 'unwired'",
                        f"set interfaces ethernet {p['name']} disable"]
        return out

    def provider(self, n):
        second = bool(self.prov2) and n["name"] in self.prov2["nodes"]
        prov = self.prov2 if second else self.prov
        out = self.vyos_base(n, "the second provider" if second else "the MPLS provider", "provider",
                             "access links: one /30 per site; the provider is .1 on a customer's, .2 on a hub's (the hub anchors it)"
                             if second else "access links: one /30 per site, the provider is .1")
        pg, pl, wan = "CE", "WAN-ADDRESSES", prov["wan_net"]
        out += ["#", f"# {n['name']}: its own AS, eBGP with every site through a listen range; it carries the sites' WAN "
                     "addresses and nothing else",
                f"set policy prefix-list {pl} description 'the access links, and nothing else'",
                f"set policy prefix-list {pl} rule 10 action permit",
                f"set policy prefix-list {pl} rule 10 prefix {wan}",
                f"set policy prefix-list {pl} rule 10 le 32",
                f"set protocols bgp system-as {prov['as']}",
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

    def vyos_customer(self, n):
        """A DMVPN customer on VyOS, interoperating with the Catalyst hubs. It gets the same service a C8000v customer
        does — eBGP to the provider for its NBMA address, one mGRE tunnel registered with every hub, IKEv2/IPsec,
        iBGP with every hub — and has to match what the hubs' IOS negotiates, not what VyOS would pick:

          IPsec in tunnel mode and without PFS: the hubs' transform-set is tunnel mode and their profile sets no PFS
          (VyOS's DMVPN examples use transport mode and PFS, which an IOS responder refuses).

          Next hop = the hub. VyOS only takes a /32 on an NHRP tunnel, so the other customers' tunnel addresses — the
          next hops the hubs reflect — resolve through nothing. Routes from a hub are therefore imported with the hub
          as next hop; phase 3 still works, because the hub's redirect makes nhrpd install a shortcut (distance 10),
          which beats BGP.

          A route to the overlay through the hubs (distance 250). nhrpd only answers a resolution request if the
          requester's tunnel address resolves through NHRP; with a /32 and no connected subnet nothing does, and the
          C8000v customers' shortcuts to this one would never form. The route makes every overlay address resolve
          through a hub, and loses to any shortcut."""
        s_, wan, t = self.svc, n["wan"], n["t_idx"]
        rid_net = f"{n['router_id'].rsplit('.', 1)[0]}.0/24"
        out = self.vyos_base(n, f"DMVPN customer on VyOS ({n.get('region') or ''})", n.get("region") or "customer",
                             "ports: eth2 to the provider, eth3 the site LAN (eth1, eth4 spare) — cabled like a C8000v customer")
        out += ["#", "# the router-id",
                f"set interfaces dummy dum0 address {n['router_id']}/32",
                "set interfaces dummy dum0 description 'router-id'"]
        out += ["#", "# filters: the provider gets our access link and nothing else; the overlay carries LANs and router-ids",
                "set policy prefix-list WAN-OUT description 'offer the provider our own access link and nothing else'",
                "set policy prefix-list WAN-OUT rule 10 action permit",
                f"set policy prefix-list WAN-OUT rule 10 prefix {wan['prefix']}",
                "set policy prefix-list WAN-IN description 'accept only the provider access-link range'",
                "set policy prefix-list WAN-IN rule 10 action permit",
                f"set policy prefix-list WAN-IN rule 10 prefix {self.prov['wan_net']}",
                "set policy prefix-list WAN-IN rule 10 le 32",
                "set policy prefix-list OVERLAY-ROUTES description 'site LANs and router-ids: all the overlay carries'",
                "set policy prefix-list OVERLAY-ROUTES rule 10 action permit",
                "set policy prefix-list OVERLAY-ROUTES rule 10 prefix 192.168.0.0/16",
                "set policy prefix-list OVERLAY-ROUTES rule 10 ge 24",
                "set policy prefix-list OVERLAY-ROUTES rule 10 le 24",
                "set policy prefix-list OVERLAY-ROUTES rule 20 action permit",
                f"set policy prefix-list OVERLAY-ROUTES rule 20 prefix {rid_net}",
                "set policy prefix-list OVERLAY-ROUTES rule 20 ge 32",
                "set policy route-map OVERLAY-IN rule 10 action permit",
                "set policy route-map OVERLAY-IN rule 10 match ip address prefix-list OVERLAY-ROUTES",
                "set policy route-map OVERLAY-IN rule 10 set ip-next-hop peer-address",
                "set policy route-map OVERLAY-OUT rule 10 action permit",
                "set policy route-map OVERLAY-OUT rule 10 match ip address prefix-list OVERLAY-ROUTES"]
        out += ["#", f"# BGP AS {s_['as']}: eBGP to the provider (AS {self.prov['as']}) underneath, iBGP with every hub over the tunnel",
                f"set protocols bgp system-as {s_['as']}",
                f"set protocols bgp parameters router-id {n['router_id']}",
                "set protocols bgp parameters log-neighbor-changes",
                f"set protocols bgp timers keepalive {s_['keepalive']}",
                f"set protocols bgp timers holdtime {s_['holdtime_bgp']}",
                f"set protocols bgp address-family ipv4-unicast network {n['lan']}",
                f"set protocols bgp address-family ipv4-unicast network {n['router_id']}/32",
                f"set protocols bgp address-family ipv4-unicast network {wan['prefix']}",
                f"set protocols bgp neighbor {wan['peer_ip']} remote-as {self.prov['as']}",
                f"set protocols bgp neighbor {wan['peer_ip']} description 'provider {wan['peer']}'",
                f"set protocols bgp neighbor {wan['peer_ip']} address-family ipv4-unicast prefix-list export WAN-OUT",
                f"set protocols bgp neighbor {wan['peer_ip']} address-family ipv4-unicast prefix-list import WAN-IN",
                f"set protocols bgp peer-group HUBS remote-as {s_['as']}",
                "set protocols bgp peer-group HUBS description 'the DMVPN hubs (route reflectors)'",
                f"set protocols bgp peer-group HUBS update-source {n['tunnel_ip']}",
                "set protocols bgp peer-group HUBS address-family ipv4-unicast soft-reconfiguration inbound",
                "set protocols bgp peer-group HUBS address-family ipv4-unicast route-map import OVERLAY-IN",
                "set protocols bgp peer-group HUBS address-family ipv4-unicast route-map export OVERLAY-OUT"]
        for h in self.hubs:
            out += [f"set protocols bgp neighbor {h['tunnel_ip']} peer-group HUBS",
                    f"set protocols bgp neighbor {h['tunnel_ip']} description 'DMVPN hub {h['name']}'"]
        pref = n.get("prefer_hub")
        if pref:
            ph = self.nodes[pref]
            out += ["#", f"# the preferred hub, {pref}: its routes rank first (local-preference 200); the hubs' own prefixes are not ranked",
                    "set policy prefix-list HUB-PREFIXES description 'the hubs own LANs and router-ids'"]
            for sq in self.hub_prefix_seqs():
                out += [f"set policy prefix-list HUB-PREFIXES rule {sq['seq']} action permit",
                        f"set policy prefix-list HUB-PREFIXES rule {sq['seq']} prefix {sq['prefix']}"]
            out += ["set policy route-map OVERLAY-IN-PREFERRED rule 10 action permit",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 10 match ip address prefix-list HUB-PREFIXES",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 10 set ip-next-hop peer-address",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 20 action permit",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 20 match ip address prefix-list OVERLAY-ROUTES",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 20 set ip-next-hop peer-address",
                    "set policy route-map OVERLAY-IN-PREFERRED rule 20 set local-preference 200",
                    f"set protocols bgp neighbor {ph['tunnel_ip']} address-family ipv4-unicast route-map import OVERLAY-IN-PREFERRED"]
        out += ["#", "# the overlay through the hubs, below any shortcut: lets nhrpd answer resolution requests"]
        out += [f"set protocols static route {self.svc['overlay']} next-hop {h['tunnel_ip']} distance 250" for h in self.hubs]
        out += ["#", "# the DMVPN tunnel: mGRE (no remote), a /32 as NHRP on VyOS requires, the hubs' GRE key",
                "set interfaces tunnel tun0 encapsulation gre",
                f"set interfaces tunnel tun0 source-address {n['nbma']}",
                f"set interfaces tunnel tun0 address {n['tunnel_ip']}/32",
                f"set interfaces tunnel tun0 mtu {s_['mtu']}",
                f"set interfaces tunnel tun0 ip adjust-mss {s_['mss']}",
                f"set interfaces tunnel tun0 parameters ip key {s_['tunnel_key']}",
                "set interfaces tunnel tun0 parameters ip ttl 64",
                f"set interfaces tunnel tun0 description 'DMVPN customer (mGRE, phase 3) {self.svc['overlay']}'",
                "#", "# NHRP: registered with every hub, shortcuts on (the hubs send redirects)",
                f"set protocols nhrp tunnel tun0 network-id {s_['network_id']}",
                f"set protocols nhrp tunnel tun0 holdtime {s_['holdtime']}",
                f"set protocols nhrp tunnel tun0 mtu {s_['mtu']}",
                f"set protocols nhrp tunnel tun0 authentication {s_['nhrp_secret']}",
                "set protocols nhrp tunnel tun0 registration-no-unique",
                "set protocols nhrp tunnel tun0 shortcut"]
        out += [f"set protocols nhrp tunnel tun0 nhs tunnel-ip {h['tunnel_ip']} nbma {h['nbma']}" for h in self.hubs]
        if n.get("wan2"):
            out += self.vyos_customer_cloud2(n)
        out += ["#", "# IKEv2 / IPsec, matched to the hubs: AES-256 / SHA-256 / DH 14, ESP tunnel mode, no PFS",
                "set vpn ipsec ike-group DMVPN-IKE key-exchange ikev2",
                "set vpn ipsec ike-group DMVPN-IKE lifetime 28800",
                "set vpn ipsec ike-group DMVPN-IKE dead-peer-detection action clear",
                "set vpn ipsec ike-group DMVPN-IKE dead-peer-detection interval 10",      # as the hubs: periodic, dead in ~30 s
                "set vpn ipsec ike-group DMVPN-IKE dead-peer-detection timeout 30",
                "set vpn ipsec ike-group DMVPN-IKE proposal 1 encryption aes256",
                "set vpn ipsec ike-group DMVPN-IKE proposal 1 hash sha256",
                "set vpn ipsec ike-group DMVPN-IKE proposal 1 dh-group 14",
                "set vpn ipsec esp-group DMVPN-ESP mode tunnel",
                "set vpn ipsec esp-group DMVPN-ESP lifetime 3600",
                "set vpn ipsec esp-group DMVPN-ESP pfs disable",
                "set vpn ipsec esp-group DMVPN-ESP proposal 1 encryption aes256",
                "set vpn ipsec esp-group DMVPN-ESP proposal 1 hash sha256",
                "set vpn ipsec profile DMVPN-IPSEC authentication mode pre-shared-secret",
                f"set vpn ipsec profile DMVPN-IPSEC authentication pre-shared-secret {s_['psk']}",
                "set vpn ipsec profile DMVPN-IPSEC ike-group DMVPN-IKE",
                "set vpn ipsec profile DMVPN-IPSEC esp-group DMVPN-ESP",
                "set vpn ipsec profile DMVPN-IPSEC bind tunnel tun0"] + (["set vpn ipsec profile DMVPN-IPSEC bind tunnel tun1"] if n.get("wan2") else []) + [

                "set vpn ipsec disable-uniqreqids",
                "set vpn ipsec options disable-route-autoinstall"]
        return "\n".join(out) + "\n"

    def vyos_customer_cloud2(self, n):
        """A dual-homed VyOS customer: eBGP with the second provider on eth4, and a second tunnel (tun1) in the second
        cloud, registered with every hub; its routes rank below the first cloud's (local-preference 50)."""
        c2, w2, s_ = self.cloud2, n["wan2"], self.svc
        out = ["#", f"# the second provider ({w2['peer']}, AS {self.prov2['as']}) and the second cloud {c2['overlay']}: the backup path",
               "set policy prefix-list WAN2-OUT description 'offer the second provider our own access link to it and nothing else'",
               "set policy prefix-list WAN2-OUT rule 10 action permit",
               f"set policy prefix-list WAN2-OUT rule 10 prefix {w2['prefix']}",
               "set policy prefix-list WAN2-IN description 'accept only the second provider access-link range'",
               "set policy prefix-list WAN2-IN rule 10 action permit",
               f"set policy prefix-list WAN2-IN rule 10 prefix {self.prov2['wan_net']}",
               "set policy prefix-list WAN2-IN rule 10 le 32",
               "set policy route-map OVERLAY2-IN rule 10 action permit",
               "set policy route-map OVERLAY2-IN rule 10 match ip address prefix-list OVERLAY-ROUTES",
               "set policy route-map OVERLAY2-IN rule 10 set ip-next-hop peer-address",
               "set policy route-map OVERLAY2-IN rule 10 set local-preference 50",
               f"set protocols bgp address-family ipv4-unicast network {w2['prefix']}",
               f"set protocols bgp neighbor {w2['peer_ip']} remote-as {self.prov2['as']}",
               f"set protocols bgp neighbor {w2['peer_ip']} description 'provider {w2['peer']}'",
               f"set protocols bgp neighbor {w2['peer_ip']} address-family ipv4-unicast prefix-list export WAN2-OUT",
               f"set protocols bgp neighbor {w2['peer_ip']} address-family ipv4-unicast prefix-list import WAN2-IN",
               f"set protocols bgp peer-group HUBS2 remote-as {s_['as']}",
               "set protocols bgp peer-group HUBS2 description 'the DMVPN hubs over the second cloud (backup)'",
               f"set protocols bgp peer-group HUBS2 update-source {n['tunnel2_ip']}",
               "set protocols bgp peer-group HUBS2 address-family ipv4-unicast soft-reconfiguration inbound",
               "set protocols bgp peer-group HUBS2 address-family ipv4-unicast route-map import OVERLAY2-IN",
               "set protocols bgp peer-group HUBS2 address-family ipv4-unicast route-map export OVERLAY-OUT"]
        for h in self.hubs:
            out += [f"set protocols bgp neighbor {h['tunnel2_ip']} peer-group HUBS2",
                    f"set protocols bgp neighbor {h['tunnel2_ip']} description 'DMVPN hub {h['name']} (cloud 2)'"]
        out += [f"set protocols static route {c2['overlay']} next-hop {h['tunnel2_ip']} distance 250" for h in self.hubs]
        out += ["set interfaces tunnel tun1 encapsulation gre",
                f"set interfaces tunnel tun1 source-address {n['nbma2']}",
                f"set interfaces tunnel tun1 address {n['tunnel2_ip']}/32",
                f"set interfaces tunnel tun1 mtu {s_['mtu']}",
                f"set interfaces tunnel tun1 ip adjust-mss {s_['mss']}",
                f"set interfaces tunnel tun1 parameters ip key {c2['tunnel_key']}",
                "set interfaces tunnel tun1 parameters ip ttl 64",
                f"set interfaces tunnel tun1 description 'DMVPN customer, cloud 2 (mGRE, phase 3) {c2['overlay']}'",
                f"set protocols nhrp tunnel tun1 network-id {c2['network_id']}",
                f"set protocols nhrp tunnel tun1 holdtime {s_['holdtime']}",
                f"set protocols nhrp tunnel tun1 mtu {s_['mtu']}",
                f"set protocols nhrp tunnel tun1 authentication {s_['nhrp_secret']}",
                "set protocols nhrp tunnel tun1 registration-no-unique",
                "set protocols nhrp tunnel tun1 shortcut"]
        out += [f"set protocols nhrp tunnel tun1 nhs tunnel-ip {h['tunnel2_ip']} nbma {h['nbma2']}" for h in self.hubs]
        return out

    # ---- C8000v: Network-as-Code ----------------------------------------------------------------------------------
    def tunnel_cli(self, n, cloud=1):
        """Tunnel0 in the first cloud (over the first provider, from Gi2), or Tunnel1 in the second (over the second
        provider, from Gi4): the same design with the second cloud's addresses, network-id and tunnel key."""
        hub = n["role"] == "hub"
        c2 = cloud == 2
        tun, nbma = ("tunnel2_ip", "nbma2") if c2 else ("tunnel_ip", "nbma")
        overlay = ipaddress.ip_network(self.cloud2["overlay"]) if c2 else self.overlay
        net_id, key = (self.cloud2["network_id"], self.cloud2["tunnel_key"]) if c2 else (self.svc["network_id"], self.svc["tunnel_key"])
        lines = [f"interface Tunnel{cloud - 1}",
                 f" description DMVPN {'hub' if hub else 'customer'}{', cloud 2 (backup)' if c2 else ''} (mGRE, phase 3)",
                 f" ip address {n[tun]} {overlay.netmask}",
                 " no ip redirects",
                 f" ip mtu {self.svc['mtu']}",
                 f" ip tcp adjust-mss {self.svc['mss']}",
                 f" ip nhrp authentication {self.svc['nhrp_secret']}",
                 f" ip nhrp network-id {net_id}",
                 f" ip nhrp holdtime {self.svc['holdtime']}"]
        if hub:
            lines.append(" ip nhrp map multicast dynamic")
            # the other hubs are static entries, not registrations: hub-to-hub iBGP needs their NBMA, and no hub is
            # another hub's client
            for h in self.hubs:
                if h is not n:
                    lines += [f" ip nhrp map {h[tun]} {h[nbma]}", f" ip nhrp map multicast {h[nbma]}"]
            lines.append(" ip nhrp redirect")
        else:
            lines += [f" ip nhrp nhs {h[tun]} nbma {h[nbma]} multicast" for h in self.hubs]
            lines.append(" ip nhrp shortcut")
        lines += [f" tunnel source GigabitEthernet{4 if c2 else 2}",
                  " tunnel mode gre multipoint",
                  f" tunnel key {key}",
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
            # the hub is the next hop of everything it reflects to a customer: the first packets to another site enter
            # the cloud at a hub, whose NHRP redirect builds the shortcut (phase 3) — and a route never points at an
            # address the customer cannot reach, such as another site's second-cloud tunnel while its first is down
            "  neighbor CUSTOMERS next-hop-self all",
            " exit-address-family",
        ] + ([] if not n.get("wan2") else [
            # the second cloud: its own peer-group, so its routes can be ranked below the first cloud's
            " neighbor CUSTOMERS2 peer-group",
            f" neighbor CUSTOMERS2 remote-as {a}",
            " neighbor CUSTOMERS2 description DMVPN customers over cloud 2 (dynamic, listen range)",
            f" neighbor CUSTOMERS2 timers {k} {h}",
            f" bgp listen range {self.cloud2['overlay']} peer-group CUSTOMERS2",
            " address-family ipv4",
            "  neighbor CUSTOMERS2 activate",
            "  neighbor CUSTOMERS2 route-reflector-client",
            "  neighbor CUSTOMERS2 route-map OVERLAY2-IN in",
            "  neighbor CUSTOMERS2 route-map OVERLAY out",
            "  neighbor CUSTOMERS2 next-hop-self all",
            " exit-address-family",
        ])) + "\n"

    def hub_vips(self, n):
        """The VIPs of the applications a hub hosts (applications.json), as /32 secondaries on Loopback10: the addresses
        the customers' application checks probe. Network-as-Code has no secondary addresses, so this is a template."""
        vips = sorted((a["id"], a["vips"][n["name"]]) for a in self.inv.get("applications") or [] if n["name"] in a.get("vips", {}))
        if not vips:
            return None
        return "\n".join(["interface Loopback10"] + [f" ip address {v} 255.255.255.255 secondary" for _, v in vips]) + "\n"

    def device(self, n):
        hub = n["role"] == "hub"
        wan = n["wan"]
        ethernets = [{"type": "GigabitEthernet", "id": "2", "description": f"WAN: {wan['peer']} {wan['peer_port']} (provider AS {self.prov['as']})",
                      "shutdown": False,
                      "ipv4": {"address": addr(wan["ip"]), "address_mask": mask(wan["prefix"])}}]
        w2 = n.get("wan2")
        if w2:
            ethernets.append({"type": "GigabitEthernet", "id": str(w2["num"]),
                              "description": f"WAN 2: {w2['peer']} {w2['peer_port']} (provider AS {self.prov2['as']})",
                              "shutdown": False, "ipv4": {"address": addr(w2["ip"]), "address_mask": mask(w2["prefix"])}})
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
        if w2:            # the second provider underneath, and the second cloud's iBGP over Tunnel1
            neighbors.append({"ip": w2["peer_ip"], "remote_as": self.prov2["as"], "description": f"provider {w2['peer']}",
                              "timers_keepalive": k, "timers_holdtime": h})
            af_neighbors.append({"ip": w2["peer_ip"], "activate": True,
                                 "route_maps": [{"direction": "in", "name": "WAN2-IN"}, {"direction": "out", "name": "WAN2-OUT"}]})
            for x in overlay_peers:
                neighbors.append({"ip": x["tunnel2_ip"], "remote_as": self.svc["as"], "description": f"DMVPN hub {x['name']} (cloud 2)",
                                  "timers_keepalive": k, "timers_holdtime": h})
                af_neighbors.append({"ip": x["tunnel2_ip"], "activate": True, "route_maps": [
                    {"direction": "in", "name": "OVERLAY2-IN"}, {"direction": "out", "name": "OVERLAY"}]})
        pref = n.get("prefer_hub") if not hub else None
        rm_in = (lambda x: f"OVERLAY-{x['name'].upper()}") if pref else (lambda x: "OVERLAY")
        af_neighbors += [{"ip": x["tunnel_ip"], "activate": True,
                          "route_maps": [{"direction": "in", "name": rm_in(x)}, {"direction": "out", "name": "OVERLAY"}]}
                         for x in overlay_peers]
        networks = [{"network": n["router_id"], "mask": "255.255.255.255"},
                    {"network": str(lan.network_address)} if classful(n["lan"]) else
                    {"network": str(lan.network_address), "mask": str(lan.netmask)},
                    {"network": str(ipaddress.ip_network(wan["prefix"]).network_address), "mask": mask(wan["prefix"])}]
        if w2:
            networks.append({"network": str(ipaddress.ip_network(w2["prefix"]).network_address), "mask": mask(w2["prefix"])})

        templates = [f"tunnel0_{n['name']}"] + ([f"tunnel1_{n['name']}"] if w2 else []) + ([f"bgp_hub_{n['name']}"] if hub else [])
        if hub and self.hub_vips(n):
            templates.append(f"vips_{n['name']}")
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
                ] + ([{"name": "HUB-PREFIXES", "description": "the hubs' own LANs and router-ids: reached at the hub itself",
                       "seqs": self.hub_prefix_seqs()}] if pref else [])
                  + ([{"name": "WAN2-OUT", "description": "offer the second provider our own access link to it and nothing else",
                       "seqs": [{"seq": 10, "action": "permit", "prefix": w2["prefix"]}]},
                      {"name": "WAN2-IN", "description": "accept only the second provider's access-link range",
                       "seqs": [{"seq": 10, "action": "permit", "prefix": self.prov2["wan_net"], "less_equal": 32}]}] if w2 else []),
                "route_maps": [
                    {"name": "WAN-OUT", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN-OUT"]}}]},
                    {"name": "WAN-IN", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN-IN"]}}]},
                    {"name": "OVERLAY", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["OVERLAY-ROUTES"]}}]},
                ] + self.prefer_route_maps(n, pref) + self.cloud2_route_maps(n, hub),
                "routing": {"bgp": {
                    "as_number": self.svc["as"], "router_id": n["router_id"], "log_neighbor_changes": True,
                    "neighbors": neighbors,
                    "address_family": {"ipv4_unicast": {"neighbors": af_neighbors, "networks": networks}},
                }},
            },
        }

    # ---- a customer's preferred hub -------------------------------------------------------------------------------
    def hub_prefix_seqs(self):
        seqs = []
        for i, h in enumerate(self.hubs):
            seqs.append({"seq": 10 + 10 * i, "action": "permit", "prefix": h["lan"]})
            seqs.append({"seq": 15 + 10 * i, "action": "permit", "prefix": f"{h['router_id']}/32"})
        return seqs

    def prefer_route_maps(self, n, pref):
        """A customer that prefers a hub ranks the preferred hub's copy of the other customers' routes first
        (local-preference 200 against 100), with that hub as next hop — so the first packets enter the cloud there, until
        the hub's NHRP redirect builds the shortcut. (Every hub now sends itself as next hop anyway; the explicit next hop
        keeps the policy right on its own.) The hubs' own prefixes keep their next hop: a hub's LAN is reached at
        that hub, whichever hub reflected the route. One route-map per hub, bound inbound on that hub's session."""
        if not pref:
            return []
        out = []
        for h in self.hubs:
            out.append({"name": f"OVERLAY-{h['name'].upper()}", "entries": [
                {"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["HUB-PREFIXES"]}},
                {"seq": 20, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["OVERLAY-ROUTES"]},
                 "set": {"ipv4_next_hop_addresses": [h["tunnel_ip"]], "local_preference": 200 if h["name"] == pref else 100}}]})
        return out

    def cloud2_route_maps(self, n, hub):
        """The second cloud ranks below the first: local-preference 50 on everything learned over it, on a hub and on
        a customer alike. (The next hop needs no rewriting: a hub sends itself as next hop to its customers.)"""
        if not n.get("wan2"):
            return []
        return [{"name": "WAN2-OUT", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN2-OUT"]}}]},
                {"name": "WAN2-IN", "entries": [{"seq": 10, "operation": "permit", "match": {"ipv4_address_prefix_lists": ["WAN2-IN"]}}]},
                {"name": "OVERLAY2-IN", "entries": [{"seq": 10, "operation": "permit",
                 "match": {"ipv4_address_prefix_lists": ["OVERLAY-ROUTES"]}, "set": {"local_preference": 50}}]}]

    def nac_devices(self):
        c8k = [n for n in self.hubs + self.spokes if n.get("platform", "c8000v") == "c8000v"]
        templates = [{"name": f"tunnel0_{n['name']}", "type": "cli", "content": self.tunnel_cli(n)} for n in c8k]
        templates += [{"name": f"tunnel1_{n['name']}", "type": "cli", "content": self.tunnel_cli(n, 2)} for n in c8k if n.get("wan2")]
        templates += [{"name": f"bgp_hub_{n['name']}", "type": "cli", "content": self.hub_bgp_cli(n)} for n in self.hubs]
        templates += [{"name": f"vips_{n['name']}", "type": "cli", "content": self.hub_vips(n)} for n in self.hubs if self.hub_vips(n)]
        doc = {"iosxe": {"templates": templates, "devices": [self.device(n) for n in c8k]}}
        head = ("---\n# GENERATED by tools/render.py from `lab.sh inventory` — do not edit by hand.\n"
                "# Per-router facts only; what every router shares is in device_groups.nac.yaml and global.nac.yaml.\n")
        return head + yaml.safe_dump(doc, sort_keys=False, width=120, default_flow_style=False)


def render_all(inv):
    r = Renderer(inv)
    out = {}
    for n in inv["nodes"]:
        if n["role"] in ("hub", "spoke") and n.get("platform", "c8000v") == "vyos":
            out[f"nodes/{n['name']}/vyos_config.txt"] = r.vyos_customer(n)
        elif n["role"] in ("hub", "spoke"):
            out[f"nodes/{n['name']}/iosxe_config.txt"] = r.day0(n)
        elif n["role"] == "provider":
            out[f"nodes/{n['name']}/vyos_config.txt"] = r.provider(n)
    out["nac/data/devices.nac.yaml"] = r.nac_devices()
    return out


if __name__ == "__main__":
    inv = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
    for path, text in render_all(inv).items():
        print(f"===== {path} =====\n{text}")
