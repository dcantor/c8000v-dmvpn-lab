# c8000v-dmvpn-lab — DMVPN on Catalyst 8000v over a simulated MPLS provider

Three Catalyst 8000v **hubs** (East, Central, West) and three C8000v **customer** routers form one DMVPN phase 3
cloud. Underneath, a VyOS router plays the **MPLS provider**: every hub and customer peers eBGP with it, and it
carries their WAN addresses — nothing else. Behind every customer router sits a small Alpine **host**, and every host
pings every other one across the overlay. Built and configured as code: libvirt VMs (`lab.sh`), one renderer
(`tools/render.py`), Cisco Network-as-Code / Terraform for the C8000vs (`nac/`), and Robot Framework validation
(`tests/`).

```
        hub-east            hub-central            hub-west       C8000v · Tunnel0 172.28.0.1-.3 · NHRP servers + BGP route reflectors
       Lo10 .71.1          Lo10 .72.1            Lo10 .73.1
          Gi2 .2              Gi2 .2                 Gi2 .2
            │ 100.70.1.0/30     │ 100.70.2.0/30        │ 100.70.3.0/30
            └───────────┐       │       ┌──────────────┘
                     eth1│  eth2│  eth3│
                      ┌──┴──────┴──────┴──┐
                      │       mpls        │   VyOS, AS 65000: one eBGP session per site (listen range, as-override)
                      └──┬──────┬──────┬──┘
                     eth4│  eth5│  eth6│
            ┌───────────┘       │       └──────────────┐
            │ 100.70.11.0/30    │ 100.70.12.0/30       │ 100.70.13.0/30
          cust1               cust2                  cust3        C8000v · Tunnel0 172.28.0.11-.13
            │Gi3 .1             │Gi3 .1                │Gi3 .1
        host-cust1          host-cust2             host-cust3     Alpine · 192.168.61/62/63.2

   overlay: mGRE Tunnel0 172.28.0.0/24 · NHRP phase 3 (hubs redirect, customers shortcut) · IKEv2 PSK + IPsec
   routing: eBGP AS 65100 <-> AS 65000 on Gi2 (underlay) · iBGP AS 65100 over Tunnel0, hubs = route reflectors
```

## Quick start

```bash
./lab.sh up               # define networks + VMs and start them (C8000vs three at a time)
./lab.sh bootstrap        # first boot only: day-0 over the consoles, license reload, wait for RESTCONF (~10-15 min)
./lab.sh nac init && ./lab.sh nac apply -parallelism=1     # push the C8000v model
./lab.sh verify           # provider sessions, NHRP, IPsec, iBGP, host ping matrix
./lab.sh test             # Robot suites -> results/<date>_<time>/
./lab.sh ssh cust1        # admin / admin (VyOS vyos / vyos, hosts lab / lab)
./lab.sh down
```

## What is where

| Path | Purpose |
|---|---|
| `lab.conf` | the inventory: roles, OOB / WAN / tunnel / LAN addressing, `LINKS`, the DMVPN service, VM shapes |
| `lab.sh` | libvirt controller — `up down bootstrap wait nac configure verify hosts test nautobot webapp status inventory console ssh log rebuild clean` |
| `lab.sh inventory` | the lab as JSON (assembled by `tools/inventory.py`): the one contract the renderer and the tests read |
| `tools/render.py` | inventory → C8000v day-0 (`nodes/<n>/iosxe_config.txt`), the provider (`nodes/mpls/vyos_config.txt`) and `nac/data/devices.nac.yaml` |
| `tools/gen_configs.py` | writes the renders; `--check` exits 1 if any is stale (a test asserts it) |
| `nac/data/device_groups.nac.yaml` | what every DMVPN router shares: the IKEv2 / IPsec suite |
| `nac/data/global.nac.yaml` | baseline: domain, SSH / AAA / VTY hardening, management ACL, banner |
| `tools/console.py`, `vyos_console.py`, `vyos_push.py`, `ios_cmd.py`, `vyos_cmd.py`, `host_cmd.py` | serial-console day-0, day-N over SSH, op-mode reads, the host ping matrix |
| `tests/suites/` | `01_management`, `02_underlay`, `03_dmvpn`, `04_routing`, `05_nac_compliance`, `06_nautobot` |
| `nautobot/` | `seed.py` (lab.conf → Nautobot), `render.py` (Nautobot → the same renderer, `--check`), `remove_customer.py`, the saved GraphQL query |
| `webapp/` | the portal: `app.py` (the runs), `customers.py` (allocate, validate, plan), `labconf.py` (edit `lab.conf`), `state.py` (what the routers are doing), `static/index.html` |
| `results/` | one folder per test run: `configs/pre-run`, `configs/post-run`, the diff, Robot report / log |

## The customers

Each customer site belongs to a (fictional) company, recorded in **`customers.json`**: company, industry, address,
phone, contact, email and account. The phone numbers are 555-01xx, which is reserved for fiction, and the email
domains are `.example`. `lab.sh inventory` carries it as each customer's `customer`.

| Node | Company | Industry |
|---|---|---|
| cust1 (East) | Harborview Dental Group | Healthcare — dental clinics |
| cust2 (Central) | Prairie Grain Logistics | Transportation & logistics |
| cust3 (West) | Cascade Outdoor Supply | Retail — outdoor equipment |

- **Nautobot:** a tenant per company in the tenant group `c8000v-dmvpn-lab customers`, with custom fields
  `customer_industry`, `customer_address`, `customer_phone`, `customer_contact`, `customer_email` and
  `customer_account`. The tenant is assigned to the customer's router and LAN host. The lab has its own tenant group,
  so no other lab's customer clean-up can touch these tenants.
- **Portal:** the company is on the customer's box in the Network map and in its details (also shown for its host).
  It's in the cloud table and the PDF, where the company name is the title.
- **Adding a customer:** the portal proposes a new fictional company, which you can edit, and writes it to
  `customers.json`. Removing the customer removes it.

## The applications

The provider hosts ten (fictional) applications at its hubs, and the customers subscribe to them. The catalogue is
in **`applications.json`**: ID, name, description, URL (`*.apps.c8dlab.example`), protocol and port, and the hubs
that host it with a **VIP** at each, taken from that hub's LAN. Each customer's subscriptions are in its `customers.json`
entry (`applications`).

| ID | Application | Hosted at |
|---|---|---|
| APP-1001 | Unified Communications (VoIP) | East, Central, West |
| APP-1002 | Email & Collaboration | East, West |
| APP-1003 | ERP — Finance & Procurement | Central |
| APP-1004 | Point of Sale Backend | Central, West |
| APP-1005 | Electronic Health Records | East |
| APP-1006 | Fleet Tracking & Telematics | Central |
| APP-1007 | Inventory & Warehouse Management | Central, West |
| APP-1008 | Secure File Transfer | East, Central |
| APP-1009 | Video Surveillance (NVR) | West |
| APP-1010 | Identity & Single Sign-On | East, Central, West |

- **Nautobot:** Nautobot 3's core load-balancer model holds them. Each application gets a **Virtual Server** at every
  hub that hosts it (18 in all). The Virtual Server has its VIP (an IP address with role `vip`), port and protocol,
  its hub as the device, and custom fields `application_id`, `application_name`, `application_url` and
  `application_description`. A many-to-many **relationship**, "Application subscriptions", links each customer's
  tenant to the Virtual Servers of the applications it subscribes to.
- **Map:**
  - Hubs are captioned with how many applications they host. In a customer's own view, the caption is the IDs of the
    customer's applications that hub serves.
  - A customer's details list its applications (URL, port, hubs and VIPs).
  - A hub's details list what it hosts and which customers subscribe.
- **PDF:** a second page, headed with the company name, lists its applications and then the technical details.
- **Adding a customer:** the portal's wizard has a checkbox per application. Email and SSO are ticked by default.
- **Not routed:** the VIPs are recorded, not configured. Only each hub LAN's `.1` answers on the routers.

## Nautobot is the source of truth

`./lab.sh nautobot seed` writes the lab into the shared Nautobot (the `nms` VM of the cat9000v lab,
http://10.0.0.10:8080):
- **Locations:** site `c8000v-dmvpn-lab` with regions `c8d-east`, `c8d-central` and `c8d-west`.
- **Devices:** every device under its libvirt name (`c8d-hub-east`, …), because Nautobot names are global and
  `vyos-dmvpn` already has a `hub-east`.
- **Interfaces:** every interface with its lab MAC, including Loopback0, Loopback10 and Tunnel0.
- **IPAM:** every address, and the prefixes with roles.
- **Cables:** a cable per link.
- **BGP:** AS 65100 and AS 65000, a routing instance per router, and a peering per session (hub↔hub,
  hub (rr)↔customer (rr-client), site (customer)↔provider).
- **Config context:** the service data that `tools/render.py` reads.

`./lab.sh nautobot render --check` rebuilds the inventory from the saved GraphQL query `c8000v-dmvpn-lab-model`,
hands it to the *same* renderer, and compares all eight rendered files byte for byte:

```
$ ./lab.sh nautobot render --check
nac/data/devices.nac.yaml: Nautobot == lab.conf (933 lines)
nodes/cust1/iosxe_config.txt: Nautobot == lab.conf (43 lines)
...
nodes/mpls/vyos_config.txt: Nautobot == lab.conf (68 lines)
```

The seed is idempotent (`0 changes (already in sync)` on a second run), and `seed --check` is a dry run that exits 1
on drift. The pre-shared key and NHRP secret are deliberately not in Nautobot; they stay in `lab.conf`.

## The provisioning portal

`./lab.sh webapp` serves **http://192.168.50.231:8094** (Swagger at `/docs`), built on the shared
[lab-portal](https://github.com/dcantor/lab-portal) run engine and registered with the lab hub on :8088.

| View | What it does |
|---|---|
| The cloud | every C8000v with what it is *actually* doing: NHRP registrations (hub) or NHS (customer), IPsec sessions, overlay BGP, the provider session, live customer-to-customer shortcuts; the provider's eBGP customers; a health line |
| Network map | the topology drawn from the routers' live state. Hubs across the top, the provider as the MPLS band, the customers and their hosts below. Toggle layers for provider links (coloured by eBGP), DMVPN tunnels (per customer per hub, coloured by NHRP registration), the hubs' static mesh, live phase 3 shortcuts, hosts and labels. Click a node for its addresses and live state, with one-click `show dmvpn`, `show crypto session brief`, `show ip bgp summary`, … on a router, or a ping to every other LAN host on a host (`GET /api/hosts/{host}/ping?target=`). The output opens in a full-width box under the map. On a customer: **Show only its view**, which drops the other customers and colours the hubs and provider by what that customer sees; its live shortcuts become labelled stubs. Also **Download PDF**: the portal's headless Chrome (Playwright, system `google-chrome`) prints that view with the customer's details as one A4 landscape page (`GET /api/customers/{name}/map.pdf`) |
| Provision | add a customer, remove one, deploy the model, dry run (terraform plan + Nautobot check), run the tests |
| Runs | every run with its steps, log and test report; a failed run resumes from the step that failed |

**Adding a customer never touches a hub.** Its pipeline:

```
validate → lab.conf + render → the C8000v and its host → day-0 (license reload) → terraform apply
        → the provider's new link → Nautobot → verify → tests
```

Terraform adds resources on the new router only. The customer registers with NHRP, arrives on the hubs' BGP
listen range, and reaches the provider on its listen range. Every value in the wizard is the next free one: `custN`,
index `10+N` (which fixes `100.70.<idx>.0/30`, `172.28.0.<idx>` and `10.255.5.<idx>`), `10.5.0.2N`, and
`192.168.6N.0/24`. **Removing one** works in reverse:
1. Terraform forgets the router's resources.
2. The VMs are deleted.
3. The customer is taken out of `lab.conf`, and Terraform re-applies. No router changes; this only refreshes
   Terraform's local copy of the model, which would otherwise show up as drift.
4. The provider's port is released: the push removes its address and disables it.
5. The customer is removed from Nautobot.

## Monitoring

The shared stack on the NMS (`lab-portal/monitoring`: Prometheus, VictoriaMetrics, VictoriaLogs, vmalert, Grafana) covers
this lab. The NMS sits on `c8d-oob` as **10.5.0.10**; that sixth NIC is in the `cat9000v` lab's NMS definition.

| Source | How it arrives |
|---|---|
| C8000vs | IOS-XE has no exporter, so the **portal measures them**. Its live state (NHRP, IPsec, BGP, CPU over SSH) is refreshed every minute in the background and served on `/metrics` as `lab_dmvpn_*`, `lab_bgp_*`, `lab_ipsec_*` and `lab_router_cpu_pct` |
| C8000v syslog | `logging host 10.5.0.10 vrf Mgmt-vrf transport udp port 5514` plus `origin-id hostname` (NAC `global.nac.yaml`) → VictoriaLogs |
| Provider (`mpls`) | node-exporter `:9100` and frr-exporter `:9342` (scraped), Telegraf push to VictoriaMetrics, syslog to VictoriaLogs |
| LAN hosts | node-exporter `:9100` |
| Discovery | Prometheus job `c8000v-dmvpn-lab` reads the portal's `/api/sd` (http://10.5.0.1:8094/api/sd) |

- **Alerts:** `DmvpnCustomerNotRegistered`, `DmvpnProviderSessionDown`, `DmvpnOverlayBgpDown`, `DmvpnRouterUnreachable`,
  `DmvpnLanHostDown`, `DmvpnRouterCpuHigh` (metrics), and `DmvpnBgpNeighborDownLogged`, `DmvpnNhsDownLogged`,
  `DmvpnIkeSaDownLogged` (syslog). Each is gated on the lab running.
- **Tested:** disabling cust3's provider port raised `DmvpnProviderSessionDown` for cust3, and re-enabling it cleared it.
- **Dashboard:** "C8000v DMVPN: overview" at http://192.168.50.231:3001/d/c8000v-dmvpn-lab-overview.
- **The portal is a service:** `systemctl --user status c8d-webapp`. Prometheus expects portals to be up at all times.

## Design notes

- **One renderer.** `lab.conf` → `lab.sh inventory` → `tools/render.py` produces every configuration file. Nothing
  else writes configuration, so a fact (an address, an AS, a key) is stated once.
- **The provider carries WAN addresses only.** Each router offers AS 65000 exactly its own Gi2 /30 (`WAN-OUT`) and
  accepts only `100.70.0.0/16` (`WAN-IN`). The provider accepts the sites through `bgp listen range` and applies
  `as-override` because every site is AS 65100. There are no static or default routes in the global table, so a lost
  access link withdraws that site's NBMA address everywhere within the 9-second hold time.
- **The planes are kept apart.** The overlay route-map `OVERLAY` passes only site LANs (`192.168.x.0/24`) and
  router-ids (`10.255.5.x/32`), in and out, so a WAN /30 can never be learned through the tunnel it is the source of.
- **Three hubs, one cloud.** Every customer lists all three hubs as NHS (`ip nhrp nhs … nbma … multicast`) and peers
  iBGP with all three. The hubs know each other by static NHRP maps and peer iBGP as non-clients.
- **Adding a customer never touches a hub or the provider.** Hubs accept customers with `bgp listen range
  172.28.0.0/24 peer-group CUSTOMERS`; the provider accepts them on `100.70.0.0/16`, and has eight customer ports
  pre-wired (eth4-eth11) so its VM never needs redefining.
- **Phase 3.** Hubs `ip nhrp redirect`, customers `ip nhrp shortcut`. The hubs reflect routes with the originating
  router's tunnel address as next hop; the first packets go through a hub, the redirect triggers a resolution, and a
  direct IPsec tunnel forms — `03_dmvpn` and `04_routing` prove it with a traceroute whose first hop is the far
  customer.
- **NAC gaps, handled as CLI templates.** The nac-iosxe 0.1.0 model has no tunnel NHRP fields and no BGP listen
  range, so Tunnel0 and the hubs' `CUSTOMERS` peer-group are raw CLI templates generated per router. Terraform cannot
  see drift inside those, so `05_nac_compliance` compares them with the running configuration directly. IPsec is
  tunnel mode because the transform-set model has no `mode transport`.
- **VM names are prefixed** `c8d-` in libvirt, because `vyos-dmvpn` already owns `hub-east`, `hub-central`,
  `hub-west` and `mpls`. Everything else uses the short names.

## Addressing

| Purpose | Block |
|---|---|
| OOB management (`c8d-oob`, host 10.5.0.1) | `10.5.0.0/24`: hubs .11–.13, customers .21–.23, mpls .31, hosts .41–.43 |
| Router ↔ provider | `100.70.<idx>.0/30`: provider .1, router .2 (the NBMA address) — idx 1–3 hubs, 11–13 customers |
| DMVPN overlay (Tunnel0) | `172.28.0.0/24`: `.<idx>` |
| Router-ids (Loopback0) | `10.255.5.<idx>`; the provider `10.255.5.254` |
| Site LANs | customers `192.168.61/62/63.0/24` (Gi3 .1, host .2); hubs `192.168.71/72/73.0/24` on Loopback10 |

Kept clear of the other labs on this host: consoles `55xx`, MACs `52:54:00:c9:<idx>:<port>`, UDP base `46000`.

## Gotchas carried over from the other C8000v labs

- **License boot level**: a fresh C8000v has no `crypto` CLI until `license boot level network-advantage addon
  dna-advantage` is set and the router reloads; `lab.sh bootstrap` does that once.
- `network 192.168.x.0 mask 255.255.255.0` is stored classful by IOS — the renderer models it without a mask.
- `ip nhrp redirect` / `ip nhrp shortcut` are defaults on mGRE in 17.15 and show only in `show running-config all`.
- RESTCONF answers 502 for a few minutes after boot; `lab.sh wait` / `bootstrap` poll for a real 200.
- Apply with `-parallelism=1` to avoid `configuration database is locked` (409) races.

## Requirements

libvirt / qemu with your user in `libvirt`, `genisoimage`, `socat`, Terraform ≥ 1.9 (`~/.local/bin`), Python 3 (venv
created by `tests/setup.sh`). The base images are shared with the other labs: the C8000v 17.15.06 qcow2 and
`vyos-base.qcow2` from `cat8000v-ipsec`, `alpine-host.qcow2` from `srv6-core`. Footprint: about 26 GB RAM and 17 vCPUs
(6 × 2 vCPU / 4 GB C8000v, 1 GB VyOS, 3 × 256 MB hosts).
