# c8000v-dmvpn-lab — DMVPN on Catalyst 8000v over two simulated providers

Three Catalyst 8000v **hubs** (East, Central, West) and three C8000v **customer** routers form one DMVPN phase 3
cloud. Underneath, a VyOS router plays the **MPLS provider**: every hub and customer peers eBGP with it, and it
carries their WAN addresses — nothing else. A second VyOS router, **mpls2**, is a second provider: every hub and any
**dual-homed** customer also connects to it and runs a second, backup DMVPN cloud over it (see *The second provider and
dual-homed customers*). Behind every customer router sits a small Alpine **host**, and every host
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
   provider 2: mpls2 (AS 65010, 100.71.0.0/16) on every hub's Gi4 and a dual-homed customer's port 4 · Tunnel1 172.29.0.0/24
```
The drawing shows the original three customers; the portal's Network map draws the lab as it is now.

Version and changes: [`VERSION`](VERSION) and [`CHANGELOG.md`](CHANGELOG.md), which uses semantic versioning. Each release is tagged `v<version>`.

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
| `tools/console.py`, `vyos_console.py`, `vyos_push.py`, `vyos_ssh.py`, `ios_cmd.py`, `vyos_cmd.py`, `host_cmd.py` | serial-console day-0, day-N over SSH, op-mode reads, the host ping matrix |
| `tests/suites/` | `01_management`, `02_underlay`, `03_dmvpn`, `04_routing`, `05_nac_compliance`, `06_nautobot`, `07_portal`, `08_vyos_customers` |
| `nautobot/` | `seed.py` (lab.conf → Nautobot), `render.py` (Nautobot → the same renderer, `--check`), `remove_customer.py`, the saved GraphQL query |
| `webapp/` | the portal: `app.py` (the runs), `customers.py` (allocate, validate, plan, modify), `labconf.py` (edit `lab.conf`), `state.py` (what the routers are doing), `drift.py` (configuration drift), `sla.py` (probes and SLA reports), `backup.py` (backup / restore), `chaos.py` (simulated failures, failover timing), `changes.py` (change control), `cportal.py` (the customer portal), `static/index.html` |
| `results/` | one folder per test run: `configs/pre-run`, `configs/post-run`, the diff, Robot report / log |

## Customer router type: Catalyst 8000v or VyOS

A customer router can be a Catalyst 8000v (the default) or **VyOS**. The choice is recorded as `PLATFORM[<node>]` in
`lab.conf`, and "Add a customer" asks for it. A VyOS customer is wired the same way as a C8000v one: eth2 to the
provider, eth3 to the LAN, eth1 and eth4 spare. It gets the same service, rendered by `tools/render.py`
(`vyos_customer`) and configured over its console:
- eBGP to the provider carrying only its access link.
- An mGRE `tun0` registered with all three hubs.
- IKEv2/IPsec, and iBGP with all three hubs.

It uses 1 GB instead of 4 GB and boots in about two minutes. Network-as-Code covers only the C8000vs.

What it takes to interoperate with the IOS hubs:
- **IPsec in tunnel mode with no PFS.** That's what the hubs' transform-set and IPsec profile negotiate. VyOS's own
  DMVPN examples use transport mode and PFS, which the hubs refuse.
- **No ESN in the IKE proposal.** VyOS always renders each IKE proposal with a `-noesn` variant, and strongSwan 6
  then sends a Sequence Numbers (type 5) transform in the IKE SA. IOS-XE 17.15 drops that IKE_SA_INIT as malformed
  without replying ("Transform type 5 invalid for protocol id 1"). `tools/vyos_ike_noesn.sh` is a VyOS post-commit
  hook that removes the ESN variants from the IKE proposals only and reloads strongSwan. `tools/vyos_hooks.py`
  installs it at bootstrap and on every `configure`, and it then runs after every commit, including the one at boot.
- **The hub as next hop.** VyOS only accepts a /32 on an NHRP tunnel, so the other customers' tunnel addresses (the
  next hops the hubs reflect) aren't reachable directly. Routes from a hub are imported with the hub as next hop.
  Phase 3 still works: the hub's redirect installs an NHRP shortcut (distance 10), which beats BGP.
- **A route to the overlay through the hubs (distance 250).** This is what lets nhrpd answer resolution requests from
  the C8000v customers, so their shortcuts to the VyOS customer form too.

Suite `08_vyos_customers` checks all of this from the VyOS side (FRR, strongSwan) and skips when there's no VyOS
customer. The other suites run the IOS checks against the C8000vs and check reachability to every router whatever its
platform. On the map, a VyOS customer is marked, and its details show the platform and VyOS commands (`show ip nhrp …`,
`show vpn ipsec sa`, …). Nautobot models it with platform `vyos`, `dum0` for the router-id and `tun0`.

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
| Network map | the topology drawn from the routers' live state. Hubs across the top, the provider as the MPLS band, the customers and their hosts below. Toggle layers for provider links (coloured by eBGP), DMVPN tunnels (per customer per hub, coloured by NHRP registration), the hubs' static mesh, live phase 3 shortcuts, hosts and labels. Click a node for its addresses and live state, a **show configuration** button next to the show commands (a C8000v's `show running-config`, the provider's `show configuration commands`, a host's network set-up; `GET /api/config/{node}`, with Copy and Download), one-click `show dmvpn`, `show crypto session brief`, `show ip bgp summary`, … on a router, or, on a host, one **ping all hosts** button that pings every other LAN host at once and reports each result and an `n / n reachable` summary (`GET /api/hosts/{host}/ping?target=`, one call per target). Also on a host, **trace path** to another host draws the real path on the map (`GET /api/hosts/{host}/traceroute?target=`), and **watch the shortcut form** first clears the two routers' shortcut (`POST /api/hosts/{host}/path/reset`), then traces until phase 3 has rebuilt it: the path through a hub in amber, then the direct shortcut in violet. The output opens in a full-width box under the map. **Export** downloads the map as drawn (layers, customer view) as SVG or PNG, in the light or the dark theme. A router whose running configuration differs from the model carries a **≠** badge, with the differences in its details. On a customer: **Show only its view**, which drops the other customers and colours the hubs and provider by what that customer sees; its live shortcuts become labelled stubs. Also **Download PDF**: the portal's headless Chrome (Playwright, system `google-chrome`) prints that view with the customer's details (`GET /api/customers/{name}/map.pdf`): page one the map and the company, page two its applications, page three this month's service report |
| Resilience | **simulate a failure** — a hub fails, a hub loses its first provider, a provider fails, a customer's circuit is cut, a customer's tunnel goes down — see it on the map (⚡, a red banner with Restore) and put it back; **measure failover**: a run that pings every host and hub LAN from every host five times a second, puts the fault in, holds it, takes it out, and reports every flow's outage (unaffected / failed over / cut off / hit on restore) with a timeline (`/api/faults`, `/api/failover`) |
| SLA | per customer, over the last 24 h / 7 d / 30 d or this month: availability (registered with at least one hub), registration with every hub, latency to each hub (average, 95th percentile, worst) and loss, against the lab's targets, with charts (`GET /api/customers/{name}/sla?window=`) |
| Provision | add a customer, **modify** one, remove one, deploy the model, dry run (terraform plan + Nautobot check), **check for drift**, **fix drift**, **back up the lab**, **restore from a backup**, run the tests |
| Jobs (change requests) | **Change requests** above the runs: what change control is holding — approve (four eyes), reject, emergency-approve outside the window, withdraw — and the **change policy** editor |
| Jobs | every job (a run of a pipeline) with a progress bar (steps finished out of all steps, the current one counting half; striped while running, green on success, red on failure), how long it has taken and — from the median of each step in earlier runs of the same kind and router type (`GET /api/runs/estimates`) — about how long is left and when it should be done; its steps, log and test report; a failed run resumes from the step that failed |
| The cloud (drift) | a **Configuration drift** card: the latest check, per router, with Check now and Fix drift |
| Lab Tools | every tool of the lab with its link and login (portal, API, lab hub, GitHub, Nautobot with deep links, Grafana, Prometheus, VictoriaMetrics, VictoriaLogs), how to reach the management network, and a searchable table of every node: VM, management IP, SSH command, credentials, serial console, addresses, RESTCONF / NETCONF / VyOS API / exporters (`GET /api/lab-tools`) |

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

### Modifying a customer

**Provision → Modify a customer** edits a customer in place, in the Add dialog's style: its company details and
applications, its region, its preferred hub, its site LAN, or its router type. The dialog shows each change and the
run it takes before anything happens:

| What changes | What the run does |
|---|---|
| company details, applications | `customers.json`, then Nautobot (seed); no router is touched |
| preferred hub, region | `lab.conf`, re-render, Network-as-Code apply (C8000v) or a VyOS push, Nautobot |
| site LAN | the router is re-addressed; the host is rebuilt on the new LAN (its address comes from its cloud-init seed); Nautobot re-modelled |
| router type | the router is rebuilt with the same identity — name, index, addresses, ports: Terraform forgets a C8000v, the old VM is deleted, the new one boots and gets its day-0, then NAC / the VyOS push, Nautobot re-modelled |

Every modify run ends with the same verification as an add, and optionally the suites (`mode: modify` on
`POST /api/runs`, with `changes`; `POST /api/customers/{name}/modify/validate` plans without changing anything).

### A customer's preferred hub

`PREFER_HUB[<customer>]=<hub>` in `lab.conf` (the wizard's and Modify's **Preferred hub**) makes the customer's
traffic to the other customers enter the cloud at that hub until phase 3 builds the shortcut; the other hubs stay as
fallback. The customer takes the other customers' routes with the reflecting hub as next hop, and ranks the preferred
hub's copy first with BGP local-preference 200 (against 100):

- C8000v: a route-map per hub, `OVERLAY-HUB-EAST` …, inbound on that hub's session (NAC: `set ip next-hop <hub>`,
  `set local-preference`), and a prefix-list `HUB-PREFIXES`: the hubs' own LANs and router-ids keep their next hop,
  so a hub's LAN is still reached at that hub.
- VyOS: `OVERLAY-IN-PREFERRED` (next hop the peer, local-preference 200) on the preferred hub's session.

Nautobot keeps it on the customer's BGP routing instance (`extra_attributes.preferred_hub`); the map marks the
tunnel to the preferred hub, and **trace path** shows the traffic crossing it. Without an entry nothing changes.

### Configuration drift

A **drift** job (Provision → Check for drift, the drift card, or on its own every `DRIFT_INTERVAL_H` = 6 hours while
the lab runs and nothing else does) compares every router with the model and changes nothing:

- **the model**: `lab.sh nautobot render --check`, Nautobot's rendering against lab.conf's;
- **C8000v**: `terraform plan` — every resource it would create, update or destroy is drift on its router — and the
  CLI templates (Tunnel0, a hub's listen range), which Terraform only writes, against `show running-config`;
- **VyOS**: the rendered `set` lines against `show configuration commands`: missing lines, and extra lines in the
  sections the model owns (policy, BGP, NHRP, static routes, IPsec, the tunnel and dummy interfaces, data-port
  addresses).

The report (`GET /api/drift`) marks routers on the map, fills the drift card, and is exported as
`lab_config_drift{router}`. **Fix drift** re-renders, applies NAC, re-pushes the VyOS routers (their routing sections
are pushed declaratively: deleted and set again in one commit, so a line the model dropped goes), seeds Nautobot, and
checks again.

### Service levels

Every minute the portal logs in to each customer's LAN host and pings every hub's LAN five times, in parallel, and
exports the result (`lab_sla_rtt_ms`, `lab_sla_loss_ratio` per customer and hub). Prometheus on the NMS scrapes it into
VictoriaMetrics (180 days), next to `lab_dmvpn_nhs_up`, which the portal has exported since 0.5.0. The **SLA** view
and the third page of the customer PDF (this month so far) read them back: availability, registration with every
hub, latency average / 95th percentile / worst per hub, loss, and how much of the window was monitored. Targets
(`webapp/sla.py`): availability 99.9%, 95th-percentile latency 20 ms, loss 0.5%.

### Backup and restore

**Back up the lab** (`GET /api/backup`) downloads one `.tar.gz`: the intent (`lab.conf`, `customers.json`,
`applications.json`), every rendered file and the NAC data, this lab's Nautobot model (the saved GraphQL query's
answer, the customer tenants, the Virtual Servers, the subscriptions), every router's running configuration and the
Terraform state, with a manifest of SHA-256s.

**Restore from a backup** uploads it (`POST /api/backups`, the archive as the body), verifies every checksum, reads
the backup's `lab.conf` with `lab.sh inventory` in a scratch copy, and shows the plan: customers removed, built,
changed in place or rebuilt. The restore run takes away what the backup lacks, writes the intent back and re-renders
(and says if a file renders differently from the backup's copy — the renderer may have changed since), builds and
bootstraps what is new, applies NAC, pushes the provider and VyOS routers, seeds Nautobot from the restored intent, and
verifies. Nautobot is not written from the export: the seed re-creates it, which `render --check` then proves. A
backup with different hubs or provider is refused.

### The second provider and dual-homed customers

A second VyOS router, `mpls2` (AS 65010, `100.71.0.0/16`), plays another carrier. Every hub has a link into it (Gi4)
and a second mGRE tunnel, **Tunnel1**, in a second DMVPN cloud (`172.29.0.0/24`, NHRP network-id 2, tunnel key 200).
A customer can be **dual-homed** — Add a customer or Modify: *Second provider → dual-homed* — and gets a link from its
port 4 into mpls2 and a Tunnel1 of its own, registered with every hub.

- **The second cloud is the backup.** Everything learned over it carries BGP local-preference 50 (a hub's
  `CUSTOMERS2` peer-group and a customer's `OVERLAY2-IN`), so traffic moves to it only when the first provider — or a
  site's circuit into it — fails.
- **The hubs send themselves as next hop** (`neighbor CUSTOMERS / CUSTOMERS2 next-hop-self all`): a customer reaches
  another site through a hub until phase 3's redirect builds the shortcut, and a route never points at an address the
  customer cannot reach — another site's second-cloud tunnel while its first is down.
- **Wiring without restarts.** The first end of a link anchors its UDP socket pair, so the hubs anchor their links into
  mpls2 (the hub is .1) and never had to restart; mpls2 anchors a customer's link (mpls2 .1), so dual-homing an
  existing customer restarts that customer's router once (Modify: save, power off, redefine, boot).
- **Timers.** NHRP holdtime 60 s (was 300) and periodic IKE dead-peer detection (10 s, 3 retries; VyOS 10 s / 30 s):
  the first failover measurements showed dual-homed site-to-site traffic stuck for two minutes on a phase 3 shortcut
  over the failed provider; with the shorter timers it moves in about 45 s.

### Resilience: simulated failures and failover timing

**Resilience** takes part of the lab down on purpose. Every failure is reversible and never saved on a router (a
reboot, a Deploy or Fix drift also undoes it), shows on the map, and is put back automatically after 30 minutes:

| Failure | How | What the design should do |
|---|---|---|
| Hub fails | the hub's VM is frozen (`virsh suspend`) | customers keep two hubs; live shortcuts need no hub |
| Hub loses its first provider | the hub's Gi2 is shut | the other hubs carry the first cloud |
| Provider fails | the provider's VM is frozen | single-homed customers are cut off; dual-homed ones move to cloud 2 |
| Customer circuit cut | the provider's port to the customer is disabled | single-homed: cut off; dual-homed: moves to its other provider |
| Customer tunnel down | the customer's Tunnel0 / tun0 is shut | as a cut circuit, for the overlay |

**Measure failover** runs one of them as an experiment and reports every flow: from every LAN host to every other
host and to every hub's LAN, five pings a second, the outage in seconds and when it began, and a verdict — unaffected,
failed over (back while the fault was still in), cut off (back only after the fault came out), hit on restore. First
measurements (27 Sep 2026, cust1 and cust4 dual-homed, cust2, cust5, cust6 single-homed):

| Failure | Result |
|---|---|
| hub-east fails (45 s) | 29 of 35 flows unaffected; only hub-east's own LAN unreachable; healthy 56 s after it came back |
| mpls fails (60 s), timers 300 s / DPD 30 s on-demand | dual-homed → hubs failed over in 4–7 s; cust1 ↔ cust4 **cut off** (stale shortcuts over the failed provider) |
| mpls fails (60 s), timers 60 s / DPD 10 s periodic | dual-homed → hubs 9 s; cust1 ↔ cust4 **failed over in 43–49 s**; single-homed customers cut off, as designed |

After a provider returns, the C8000v customers re-register within about 20 s; the VyOS customers take two to two and
a half minutes (FRR nhrpd's registration back-off once their IPsec was cleared).

### Change control

Disruptive work needs a second person: removing, modifying or restoring customers, deploying, fixing drift, simulating
a failure and measuring a failover (the default policy; adding a customer, dry runs, tests and drift checks are never
held). Such a request answers `202` with a **change request** (CR-0001 …) instead of starting. Someone other than the
requester approves it — four eyes — or rejects it; with **change windows** on, an approved change is scheduled and the
portal starts it when the next window opens, unless the approver declares an **emergency** (with a reason). Requests
nobody decides on expire after 72 h. The requester and the approver are signed-in accounts (an operator files, an
approver decides — see *Signing in*); a customer's own requests are change requests too. `GET/PUT /api/policy`, `/api/changes`, `POST /api/changes/{id}/approve|reject|cancel`.

### The customer portal

**Customer portal link** on a customer's details gives that customer a read-only page of its own (a customer account
— see *Customer self-service* — gets the same view with requests and diagnostics), `/c/<token>` — a
random token per customer, kept on the lab host (`webapp/customer_portal/`, not in git), rotated from the same button,
and dropped when the customer is removed. The page shows its service status, its site on the map with the hubs and
providers as its service sees them, its applications, its service levels, its monthly report (PDF), **planned
maintenance** (change requests that touch it) and **incidents** (simulated failures that touch it). Everything it reads
comes from `/api/c/<token>/…`, which answers only about that customer: no other customer's name, company, addresses or
shortcuts.

### Signing in: accounts and roles

The portal asks for an account. Roles: **viewer** reads everything; **operator** also starts jobs, files change requests,
simulates failures and hands out customer links; **approver** approves or rejects change requests (never their own);
**admin** does all of that and manages accounts and the change policy (Lab Tools → Users); **customer** sees one
customer's own service and nothing else. The lab-default staff accounts are in `webapp/users.seed.json` — lab defaults
like every other credential here — and are created, hashed, in `webapp/auth/users.json` on first start; the portal
reminds an admin of any still on its default password. Sessions are signed cookies (12 h); changing a password ends
them. Open without an account: the sign-in page, `/api/version`, Prometheus' `/metrics` and `/api/sd`, a customer's
secret link, and `GET /api/runs` from the lab host (the lab hub polls it). Change requests now carry the signed-in
account, so four eyes means two accounts.

### Maintenance

A disruptive job (remove, modify, restore, deploy, fix drift, a failover measurement) or a simulated failure runs
under a **maintenance record**; an operator can also declare one (Jobs → Maintenance). While it is open its nodes —
and the routers and hosts of the customers it affects — are exported as `lab_maintenance{router} = 1`, and this lab's
alert rules in lab-portal/monitoring do not fire for them; the map and Jobs page show it; the affected customers'
portals say "maintenance in progress" instead of an incident; and the SLA leaves the time out of availability and loss
(`lab_maintenance_customer`), reporting it as minutes of announced maintenance.

### Configuration history

Every changing job (add, modify, remove or restore customers, deploy, fix drift) snapshots every router before its
first step and after its last (before the tests; if it fails midway, when it ends). The job page lists what changed per
router (+ / − lines) with the unified diffs; a router's details have **configuration history**: the jobs that changed
it. Timestamps, byte counts and VyOS password hashes are left out. Kept in `webapp/confighist/`, the newest 150 jobs.
`GET /api/runs/{id}/config`, `/api/config-history[?router=]`, `/api/config-history/{job}/{router}?which=before|after`.

### Application checks

Every hub carries the VIPs of the applications it hosts — `/32` secondary addresses on Loopback10, from a CLI template
(`vips_<hub>`; Network-as-Code has no secondary addresses), rendered from `applications.json` and, identically, from
Nautobot's Virtual Servers. Every minute each customer's LAN host pings its subscribed applications' VIPs at every
hosting hub and connects to the HTTPS ones on 443 (the hubs' HTTPS server answers; other services are only pinged).
The results are on the map (a dot and the round trip next to each VIP), in the SLA view and the PDF (availability per
application, any hub and per hub — page four), in the customer's portal, and on `/metrics` (`lab_app_up`,
`lab_app_rtt_ms`).

### Customer self-service

A customer account (Lab Tools → Users, role customer) signs in to its own view with two more pages. **Requests**: ask
for different applications, a preferred hub, or a second provider; the request becomes a change request that always
waits for an approver, and the page shows its progress (waiting, being carried out, done, declined). **Diagnostics**:
test its applications or the hubs now, from its own LAN host, or trace the path to an application's VIP — only its own
applications and the hubs, never another customer's site. The secret link stays read-only; requests need the account.

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
| Router ↔ provider | `100.70.<idx>.0/30`: provider .1, router .2 (the NBMA address) — idx 1–3 hubs, 11–16 customers |
| Router ↔ second provider (`mpls2`, 10.5.0.32) | `100.71.<idx>.0/30`: a hub is .1 (it anchors the link), mpls2 .2; a dual-homed customer is .2, mpls2 .1 |
| DMVPN overlay (Tunnel0) | `172.28.0.0/24`: `.<idx>` |
| Second cloud (Tunnel1) | `172.29.0.0/24`: `.<idx>` — the hubs and the dual-homed customers |
| Router-ids (Loopback0) | `10.255.5.<idx>`; the provider `10.255.5.254`, the second provider `.253` |
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
