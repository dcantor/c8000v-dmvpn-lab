# Changelog

All notable changes to c8000v-dmvpn-lab are recorded here, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **MAJOR:** a change that breaks how the lab is used, for example `lab.conf` keys, `lab.sh` commands or the portal API.
- **MINOR:** a new capability.
- **PATCH:** a fix to something that already existed.

While the version is 0.x, the lab's interfaces are still settling. The current version is in [`VERSION`](VERSION), in
the portal's header, and in git as a `v<version>` tag.

## [0.18.0] — 2026-09-27

### Added
- **Path view (Network map, on a host).**
  - **trace path** to another host runs a traceroute and draws the path on the map (`GET /api/hosts/{host}/traceroute`).
  - **watch the shortcut form** first clears the two routers' shortcut (`POST /api/hosts/{host}/path/reset`), then
    traces until phase 3 has built it again. The first, cold trace crosses a hub, drawn in amber. Later ones send
    traffic first (`warm=true`), so the hub redirects, and the path goes direct, drawn in violet.
- **Modify a customer (Provision).** A dialog in the Add dialog's style edits the company details, applications,
  region, preferred hub, site LAN and router type, and shows each change and the run it takes before anything happens.
  - Company details or applications: data only.
  - A new preferred hub: policy re-applied.
  - A new LAN: the router is re-addressed and the host rebuilt.
  - A new router type: rebuilt with the same identity.
  - Endpoints: `mode: modify` on `POST /api/runs`; `GET /api/customers/{name}`; `POST /api/customers/{name}/modify/validate`.
- **A customer's preferred hub.**
  - Set it with `PREFER_HUB[<customer>]` in `lab.conf`, or in the wizard and Modify.
  - The customer takes the other customers' routes with the reflecting hub as next hop, and ranks the preferred
    hub's copy first with BGP local-preference 200.
  - C8000v: a route-map per hub through Network-as-Code, plus a `HUB-PREFIXES` exception so a hub's own LAN is still
    reached at that hub. VyOS: `OVERLAY-IN-PREFERRED` on the preferred hub's session.
  - Kept in Nautobot on the customer's BGP routing instance.
  - The map draws the tunnel to the preferred hub solid and thicker.
- **Configuration drift.** A `drift` run compares every router with the model:
  - Nautobot vs lab.conf;
  - `terraform plan`, plus the CLI templates against `show running-config`, for the C8000vs;
  - the rendered `set` lines against `show configuration commands` for VyOS.
  
  It runs from Provision, from the drift card on The cloud, or on its own every 6 hours while the lab is idle
  (`DRIFT_INTERVAL_H`). Drifted routers get a ≠ badge on the map, with the differences in their details.
  - **Fix drift** (`fixdrift`): re-render; undo stray Tunnel0 lines and re-write drifted templates
    (`terraform apply -replace`); apply NAC; re-push VyOS; seed Nautobot; check again.
  - Endpoints and metrics: `GET /api/drift`, `POST /api/drift/check`, `lab_config_drift{router}`.
- **Service levels.**
  - Every minute each customer's LAN host pings every hub's LAN (`lab_sla_rtt_ms`, `lab_sla_loss_ratio`), stored in
    VictoriaMetrics.
  - A new **SLA** view shows, per customer, over 24 h, 7 d, 30 d or this month:
    - availability and registration with every hub (from `lab_dmvpn_nhs_up`);
    - latency average, 95th percentile and worst, per hub;
    - loss and monitoring coverage;
    - targets and charts.
  - Endpoint: `GET /api/customers/{name}/sla`.
  - The customer PDF gains a third page: this month's service report.
- **Backup and restore.**
  - **Back up the lab** (`GET /api/backup`) downloads one .tar.gz: the intent, every render, this lab's Nautobot model,
    the routers' running configs and the Terraform state, with SHA-256s.
  - **Restore from a backup** uploads one (`POST /api/backups`), verifies it and shows the plan (customers removed,
    built, changed or rebuilt). A `restore` run rebuilds the lab to it and verifies.
- **Run duration and ETA** on the progress bars. Each bar shows the time taken so far and the time left, estimated from
  the median of each step in earlier runs of the same kind and router type (`GET /api/runs/estimates`).
- **Map export as SVG or PNG**, in the light or the dark theme. It exports the map as drawn (layers, customer view),
  with a title, the time and a legend.
- **Tests:**
  - `04_routing`: the preferred hub's routes are best (localpref 200); no policy without a preference.
  - `07_portal`: estimates, traceroute, the shortcut forming after a reset, drift, SLA, modify planning, backup round
    trip.

### Changed
- **VyOS push is declarative for the routing sections.** Policy, BGP, NHRP, static routes, IPsec, and the tunnel and
  dummy interfaces are deleted and set again in one commit. An unchanged section changes nothing (shortcuts and BGP
  sessions stay up), and a line the model dropped goes.
- **The customer PDF has three pages.**

### Fixed
- **Adding a VyOS customer could fail at the provider step.** The failure was "Pattern not detected: 'vyos@cust6:~$
  set terminal length 0'". `tools/vyos_push.py`, `tools/vyos_cmd.py` and the test library no longer drive VyOS
  through netmiko's interactive login, which intermittently missed the prompt on a freshly booted VyOS. They use plain
  SSH exec channels instead (`tools/vyos_ssh.py`): op mode through VyOS's wrapper, configuration through vbash's
  script template.

### Lab state
- **cust6** (VyOS, Gulfstream Marine Services) was added through the portal.
- **Preferred hubs:** cust1 prefers hub-west and cust5 prefers hub-east, set with Modify.
- **cust6 round trip:** moved to 192.168.86.0/24, rebuilt as a Catalyst 8000v, then restored from a backup to VyOS on
  192.168.66.0/24.
- **Tests:** 59 of 59.

## [0.17.0] — 2026-09-27

### Added
- **Runs page: a progress bar for every run.** The list has a Progress column, and the selected run's card has a
  wider bar above its steps.
  - Progress is the finished steps out of all the run's steps. The step in progress counts as half.
  - The label reads "step 3 of 5" while running (the wide bar adds the step's title), otherwise "8 / 9 steps".
  - Colours: striped teal while running, green on success, red when failed, interrupted or cancelled, grey when
    queued. Reduced motion stops the stripes.
  - While you watch a run, the list's bars move with it.

## [0.16.0] — 2026-09-27

### Changed
- **Network map, a host's details:** one **ping all hosts** button replaces the per-host ping buttons. It pings every
  other LAN host in parallel and fills the output box as each answers: a section per target marked reachable or
  UNREACHABLE, then an `n / n reachable` summary. The API is unchanged (`GET /api/hosts/{host}/ping`, one call per
  target).

## [0.15.1] — 2026-09-27

### Fixed
- **Tests broke when customers changed.** After cust3 was removed and cust5 re-added on VyOS, the deployment's Robot
  run reported 49 of 52. All three failures were test bugs; the network was healthy (the run's own verify passed, and
  12 of 12 host pairs were reachable). The suites had customer names written into them:
  - `03_dmvpn`: the shortcut test used the fixed pairs cust1→cust2→cust3.
  - `04_routing`: the host path test used host-cust1 → host-cust3.
  - `07_portal`: expected the next customer to be "count + 1" (cust5), but the allocator correctly proposes the highest
    number + 1 (cust6), because gaps aren't reused.
  - Two more spots were tied to cust1: the collision test and a hub regex.

  Everything now comes from the inventory: `C8K_PAIRS` (a ring of the C8000v customers), `HOST_PAIR`, `NEXT_CUSTOMER`
  and the first existing customer. A test skips when the lab has too few customers for it.

### Lab state
- cust3 (Catalyst 8000v, Cascade Outdoor Supply) was removed. cust5 was removed and re-added on VyOS (Summit Ridge
  Property Management). Customers are now cust1 and cust2 (Catalyst 8000v), and cust4 and cust5 (VyOS). 52 of 52.

## [0.15.0] — 2026-09-27

### Added
- **Run screen:** the step being processed blinks, both its status bubble and its title. Pending steps are now grey
  instead of the same amber as the running one. A reduced-motion setting gets a steady outline instead of the blink.
- **Run header and Runs list:** each shows the customer's router type (Catalyst 8000v or VyOS).
- **Robot report:** new metadata, `Routers`, lists every router with its platform. A second entry, `Lab`, says that
  "c8000v-dmvpn-lab" in the report title is the lab's name, not a router type.
- **`VERSION` file and this changelog.** The portal shows the version in its header.

### Fixed
- **Step titles:** a VyOS customer's run showed Catalyst 8000v step titles ("Create and boot the C8000v…",
  "Day-0 … license level, reload, wait for RESTCONF"). The titles now follow the router being added or removed; the
  router built was always the right one.
- **VyOS commands:** "show configuration" and the VyOS `show` buttons sometimes returned 502. netmiko's interactive
  VyOS login intermittently missed the prompt. VyOS op-mode commands now run over a plain SSH exec channel through
  `vyatta-op-cmd-wrapper`, with no prompt to detect.

### Lab state
- cust4 (VyOS, Redwood Coast Brewing Co.) and cust5 (Catalyst 8000v, Summit Ridge Property Management) were added
  through the portal and are now recorded in the repository (`lab.conf`, `customers.json`, the renders).

## [0.14.0] — 2026-09-27

### Changed
- **Portal, Remove a customer:** now a dialog in the same style as Add a customer, replacing the browser prompt.
  - Pick the customer from a list showing company, router type and region.
  - Its details and company appear as read-only fields, with its applications.
  - A plan table shows what will be removed.
  - Buttons: run-the-suites checkbox, Remove, Re-check, Cancel.

## [0.13.0] — 2026-09-27

### Added
- **Add a customer: choose the router, Catalyst 8000v or VyOS.** A VyOS customer is wired the same way (port 2 to
  the provider, port 3 to its LAN) and gets the same service.
  - It uses 1 GB instead of 4 GB and boots in about two minutes.
  - It's recorded as `PLATFORM[<node>]` in `lab.conf`.
  - It's rendered by `tools/render.py` (`vyos_customer`) and configured over its console.
- **Interop with the IOS hubs:**
  - ESP in tunnel mode with no PFS.
  - The hub as BGP next hop, plus a distance-250 overlay route through the hubs, because VyOS only accepts a /32 on
    an NHRP tunnel. Phase 3 shortcuts form in both directions.
  - `tools/vyos_ike_noesn.sh`, a post-commit hook installed by `tools/vyos_hooks.py`. It removes the `-noesn`
    variants from the IKE proposals, because strongSwan 6 sends an ESN transform in the IKE SA for them and IOS-XE
    17.15 drops that IKE_SA_INIT as malformed.
- **Platform-aware everywhere:**
  - `lab.sh` and the inventory.
  - Nautobot: platform `vyos`, `dum0` and `tun0`.
  - The portal: live state from FRR and strongSwan, VyOS show commands, configuration, Lab Tools, service discovery,
    and the wizard's Router type.
  - New test suite `08_vyos_customers`.

## [0.12.1] — 2026-09-27

### Changed
- **Network map:** "show configuration" sits with the other command buttons, in the same style.

## [0.12.0] — 2026-09-27

### Added
- **Network map:** "show configuration" on every node, read live (`GET /api/config/{node}`).
  - A C8000v's `show running-config`.
  - The provider's `show configuration commands`.
  - A host's network set-up.
- **Output box:** gains a Download (.txt) button.

### Fixed
- **Tests:** a `#` in a Robot argument was read as the start of a comment.

## [0.11.0] — 2026-09-26

### Added
- **Portal, Lab Tools page** (`GET /api/lab-tools`):
  - Every tool with its link and login: the portal, the API, the lab hub, GitHub, Nautobot deep links, Grafana,
    Prometheus, VictoriaMetrics and VictoriaLogs.
  - How to reach the management network.
  - A searchable table of every node: VM, management IP, SSH, credentials, serial console, addresses, and other access.

## [0.10.0] — 2026-09-26

### Added
- **Applications:** ten hosted applications (`applications.json`), each with an ID, URL, protocol and port, its hosting
  hubs and a VIP at each hub. Each customer's subscriptions are in `customers.json`.
- **Nautobot:**
  - A load-balancer Virtual Server per application per hosting hub, with its VIP and `application_*` custom fields.
  - An "Application subscriptions" relationship from each customer tenant.
- **Map and PDF:**
  - Hub captions show the hosted applications.
  - A customer's applications and a hub's hosted applications appear in the details.
  - The PDF gets a second page listing the applications.
  - The wizard has an application checkbox for each one.

## [0.9.0] — 2026-09-26

### Added
- **Customers are companies:** fictional company data in `customers.json` (company, industry, address, 555-01xx phone,
  contact, `.example` email, account).
  - In Nautobot, a tenant per company in the group "c8000v-dmvpn-lab customers", with `customer_*` custom fields.
  - Shown on the map, in the details and in the PDF.
  - The wizard proposes a fictional company for each new customer.

## [0.8.0] — 2026-09-26

### Added
- **Network map:**
  - "Show only <customer>'s view", with the hubs and provider coloured from that customer's perspective.
  - "Download PDF" (`GET /api/customers/{name}/map.pdf`), rendered by headless Chrome from the page's print mode.

### Fixed
- **Tests:** PDF pages are counted with `pdfinfo`, because Chrome compresses its object streams.

## [0.7.0] — 2026-09-26

### Added
- **Network map:** on a host's details, ping every other LAN host (`GET /api/hosts/{host}/ping`).

## [0.6.1] — 2026-09-26

### Changed
- **Network map:** show-command output opens in its own full-width box below the map.

## [0.6.0] — 2026-09-26

### Added
- **Portal, Network map:** the topology drawn from the routers' live state.
  - Layers for provider links, DMVPN tunnels, the hubs' mesh, phase 3 shortcuts and hosts.
  - Click a node for its live state and one-click `show` commands.

## [0.5.0] — 2026-09-26

### Added
- **Monitoring on the shared NMS:**
  - The portal measures the C8000vs (`/metrics`, `/api/sd`).
  - The VyOS provider runs exporters and pushes Telegraf metrics and syslog.
  - The C8000vs send syslog through Mgmt-vrf.
  - Prometheus alerts, and a Grafana overview dashboard.
- **Portal service:** runs as the `c8d-webapp` user service.

## [0.4.1] — 2026-09-26

### Fixed
- **Removal:** a customer's removal re-applies NAC, so Terraform's local model copy matches the routers.

## [0.4.0] — 2026-09-26

### Added
- **Provisioning portal on :8094:** add and remove a customer end to end without touching a hub.

## [0.3.0] — 2026-09-26

### Added
- **Nautobot as the source of truth:**
  - `nautobot/seed.py` models the lab.
  - `nautobot/render.py` rebuilds it and renders the same configuration as `lab.conf`, byte for byte.

## [0.2.0] — 2026-09-26

### Added
- **The lab running end to end:** the provider, the underlay, DMVPN phase 3 with IKEv2/IPsec, iBGP, and the host
  ping matrix. 29 of 29 Robot tests.

### Fixed
- **Bootstrap verdict:** now judged by the helpers' own result lines.
- **Hosts:** they route the overlay.
- **Hub-to-hub tests:** hub-to-hub is a static NHRP entry protected by IPsec.

## [0.1.0] — 2026-09-26

### Added
- **Scaffold:**
  - `lab.conf`, the inventory.
  - `lab.sh`, the libvirt controller, with VMs prefixed `c8d-`.
  - `tools/render.py`, the one renderer.
  - `nac/`, Network-as-Code for the C8000vs.
  - Robot suites, the README, and the first Nautobot seed.

[0.18.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.15.1...v0.16.0
[0.15.1]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dcantor/c8000v-dmvpn-lab/releases/tag/v0.1.0
