# Changelog

All notable changes to c8000v-dmvpn-lab are recorded here, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **MAJOR:** a change that breaks how the lab is used, for example `lab.conf` keys, `lab.sh` commands or the portal API.
- **MINOR:** a new capability.
- **PATCH:** a fix to something that already existed.

While the version is 0.x, the lab's interfaces are still settling. The current version is in [`VERSION`](VERSION), in
the portal's header, and in git as a `v<version>` tag.

## [0.21.0] — 2026-09-27

### Added
- **Rotate the pre-shared key** (Provision; admin only; change control applies). A job that:
  - chooses a random 40-character key and applies it to every router;
  - sets every router's IKE sessions up again, customers first and then the hubs, one at a time, waiting for each to
    be registered again;
  - reads every IKE SA to prove each was made after the rotation with the pre-shared key.
  
  A card on Provision shows where the key comes from, its fingerprint, and when and by whom it was last rotated.
  Endpoint: `GET /api/security`.

### Changed
- **The pre-shared key is out of git.**
  - It lives in `secrets/dmvpn_psk` on the lab host; lab.conf's value is only the public default before the first
    rotation.
  - The VyOS renders carry an `@@DMVPN_PSK@@` placeholder, filled in when pushed.
  - Network-as-Code reads the key as a global variable from the git-ignored `nac/data/secrets.nac.yaml`.
  - The portal masks the key in the configurations it shows, the configuration history and drift reports.
- **The C8000v tunnel templates are applied after the rest of the Network-as-Code model** (template order 1).

### Removed
- **Certificate authentication.** It was started in this release and withdrawn at your request before it shipped.
  - The trustpoint, keys and certificate profile it had put on hub-east and cust1 were removed.
  - The certificate files and hook on cust4 were removed.
  - The lab CA was deleted.

### Lab state
- **The pre-shared key was rotated once** (CR-0021: admin, approved by approver). All 54 IKE SAs on the 8 routers were
  re-made with the new key.
- **Tests:** 73 of 73.

## [0.20.0] — 2026-09-27

### Added
- **Logins and roles.**
  - The portal now asks for an account. There are five roles:
    - viewer: reads everything;
    - operator: starts jobs, files change requests, simulates failures;
    - approver: approves or rejects change requests;
    - admin: all of that, plus accounts and the change policy;
    - customer: its own service only.
  - Passwords are PBKDF2-hashed. Sessions are HMAC-signed cookies lasting 12 h, and changing a password ends them.
  - Lab-default staff accounts come from `webapp/users.seed.json`. Admins are reminded while one keeps its default
    password.
  - A Users card in Lab Tools, a sign-in form, and the account and role in the header.
  - Change requests carry the signed-in account, so four eyes means two accounts.
  - Still open without an account: `/metrics`, `/api/sd`, `/api/version`, customer links, and `GET /api/runs` from the
    lab host (the lab hub).
  - Endpoints: `/api/auth/login|logout|me|password|users`.
- **Maintenance.**
  - Disruptive jobs and simulated failures run under a maintenance record, and operators can declare one (Jobs →
    Maintenance).
  - Its nodes, and those of the customers it affects, are exported as `lab_maintenance{router}`. This lab's alert rules
    (lab-portal/monitoring, deployed) don't fire for them.
  - The map and Jobs page show it. The affected customers' portals say "maintenance in progress" rather than calling it
    an incident.
  - The SLA leaves maintenance out of availability and loss, and reports its minutes.
  - Endpoints: `/api/maintenance`.
- **Configuration history.**
  - Every changing job snapshots every router before its first step and after its last, or at the end if it fails
    midway.
  - The job page shows per-router +/− counts and colored unified diffs. A router's details have "configuration history".
  - Endpoints: `/api/runs/{id}/config`, `/api/config-history`.
- **Application checks.**
  - The hubs carry their applications' VIPs: /32 secondaries on Loopback10, from a `vips_<hub>` CLI template. They
    render identically from Nautobot's Virtual Servers, and the drift check covers them.
  - Every minute each customer's LAN host pings its subscribed VIPs, and connects to the HTTPS ones on 443.
  - Results appear on the map (a dot and the round trip per VIP) and in the SLA view (availability per application).
    The PDF gets a fourth page for it; the customer portal shows it too.
  - Metrics: `lab_app_up`, `lab_app_rtt_ms`.
- **Customer self-service** (a customer account).
  - **Requests:** a customer asks for applications, a preferred hub, or a second provider. The request becomes a change
    request that always waits for an approver, and the customer sees its progress.
  - **Diagnostics:** it tests its applications or the hubs from its own LAN host, or traces the path to one of its
    applications. It never reaches another customer's site.
  - Endpoints: `/api/c/me/…`.
- **Jobs** show who started them ("prairie, approved by approver").
- **Tests (07_portal):** logins and roles, maintenance, configuration history, application checks, a customer account's
  view, requests and diagnostics. The suite signs in with the lab-default accounts.

### Changed
- **The customer PDF has four pages.** The applications' availability is page four.
- **The VyOS push no longer re-sets the login password** once the account has one. VyOS stored a fresh hash every time,
  so every push changed the configuration.

### Fixed
- **Two tests had never run.** The preferred-hub test (0.18.0) and the second-cloud test (0.19.0) were appended after
  their suite's keywords section, so Robot read them as keywords. They now run.
  - Both BGP best-path checks also picked the "Paths: … best #N" header, so the dual-homing check had passed without
    checking anything. They now select real path entries.
  - The C8000v and host shortcut tests now keep traffic flowing until the path is direct. Otherwise they could trace in
    the moment between the NHRP entry and the shortcut taking the traffic.
- **The SLA probe misread some results as 100% loss.** Its parallel pings printed each result in two writes, and those
  lines interleaved. This inflated the loss numbers since 0.18.0. Each result is now one write.

### Lab state
- **Accounts:** the lab-default staff accounts were created. A test customer account, "prairie" for cust2, was added;
  its password is in `webapp/auth/test-accounts.json`, on the lab host only.
- **CR-0011:** deployed the VIPs (operator, approved by approver).
- **CR-0012:** filed by the customer account and approved. cust2 subscribes to APP-1004 and prefers hub-central.
- **Tests:** 72 of 72.

## [0.19.1] — 2026-09-27

### Changed
- **The portal calls runs "Jobs".** This covers the navigation tab, the page heading, the job details, the change
  requests' links and messages, and the Provision and Resilience hints. The API is unchanged: jobs are still
  `/api/runs`, with a `mode` per kind.

## [0.19.0] — 2026-09-27

### Added
- **A second provider, and dual-homed customers.**
  - A second VyOS router, `mpls2` (AS 65010, `100.71.0.0/16`), plays another carrier. Every hub has a link into it
    (Gi4) and a second tunnel, Tunnel1, in a second DMVPN cloud (`172.29.0.0/24`, NHRP network-id 2, key 200).
  - A customer can be dual-homed from Add a customer or Modify, on either platform: a link from its port 4 into
    mpls2 and a Tunnel1 of its own. Toggling dual-homing restarts only that customer's router.
  - The second cloud is the backup: its routes carry local-preference 50.
  - The hubs anchor their links into mpls2, so none of them restarted.
  - Also covered: lab.conf, the inventory, the renderer (C8000v NAC model and templates, VyOS), Nautobot (seed and
    `render --check`, including a second peer-group on the hubs), the live state and health, the map (a second
    provider, cloud 2 tunnels and access links, a layer toggle), the tables and dialogs, and `lab.sh verify`.
- **Resilience: failure simulation.** A new Resilience view, with markers on the map and a banner with Restore.
  - Five reversible failures: a hub fails (VM frozen), a hub loses its first provider, a provider fails, a customer
    circuit is cut, a customer tunnel goes down.
  - Nothing is saved on a router, and a failure left in is put back after 30 minutes.
  - Endpoints: `GET/POST /api/faults`, `DELETE /api/faults/{id}`, `POST /api/faults/restore-all`.
- **Failover timing.** A `failover` run measures one failure as an experiment:
  - every LAN host pings every other host and every hub's LAN five times a second, measured flow by flow (35 flows
    here);
  - the fault goes in, is held, comes out, and the run waits for the lab to be healthy again.
  - Each flow gets its outage and a verdict: unaffected, failed over, cut off, or hit on restore. There are
    per-flow timelines and a control-plane timeline (`/api/failover`).
  - A failed or interrupted experiment never leaves its fault in.
- **Change control.** Removing, modifying or restoring customers, deploying, fixing drift, simulating a failure and
  measuring a failover now file a change request (CR-0001 …, HTTP 202) instead of starting.
  - Someone other than the requester approves it (four eyes) or rejects it.
  - With change windows on, an approved change is scheduled for the next window, unless it's an emergency with a
    reason.
  - Unanswered requests expire after 72 h.
  - A policy editor on Runs, your name in the header, and a "changes waiting" badge.
  - Endpoints: `/api/changes`, `/api/policy`.
  - Names are typed, not authenticated.
- **Customer portal.** A per-customer read-only link, `/c/<token>` (made, copied and rotated from the customer's
  details; dropped when the customer is removed).
  - It shows the customer's service status, its own view of the map, its applications, service levels and monthly
    report, planned maintenance (change requests that touch it) and incidents (failures that touch it).
  - Its API (`/api/c/<token>/…`) answers only about that customer: no other customer's name, company, addresses or
    shortcuts.
- **Tests:**
  - `02_underlay`: the second provider's sessions and what it carries.
  - `03_dmvpn`: the second cloud — Tunnel1 registrations and IPsec on both sides.
  - `04_routing`: next hop through a hub, the hubs' next-hop-self, cloud 2 ranked below cloud 1.
  - `07_portal`: change control with four eyes and a fault in and out, the failure catalogue, the customer portal's
    isolation.

### Changed
- **The hubs send themselves as next hop to their customers** (`neighbor CUSTOMERS next-hop-self all`).
  - A C8000v customer now reaches another site through a hub until phase 3's shortcut forms, as the VyOS customers
    already did.
  - A route never points at an address the customer cannot reach.
  - Needed for dual-homing: otherwise a single-homed customer would get another site's second-cloud address as next
    hop.
- **Timers, from the failover measurements.**
  - NHRP holdtime 300 → 60 s.
  - IKEv2 dead-peer detection 30 s × 5 on-demand → 10 s × 3 periodic (VyOS: 30/150 s → 10/30 s).
  - With the old timers, traffic between dual-homed customers stayed on a phase 3 shortcut over the failed provider
    and was cut off for about 2 minutes. Now it fails over in 43–49 s.

### Lab state
- **mpls2** was added. **cust1** (C8000v) and **cust4** (VyOS) are dual-homed.
- **cust1's applications** are now APP-1002, APP-1007 and APP-1010, changed through Modify a customer at 12:37 before this
  release (they were APP-1001, 1002, 1005, 1008 and 1010).
- **Change requests** CR-0001 to CR-0006 were filed by "claude" and approved as "claude (test approver)". That name
  was a test of the four-eyes flow, not a second person. The test suite files its own ("robot", approved as
  "robot-approver").
- **Failover measurements:**
  - hub-east fails: 29/35 flows unaffected.
  - mpls fails, old timers: dual-homed site-to-site traffic cut off.
  - mpls fails, new timers: failed over in 43–49 s.
- **Tests:** 65 of 65.

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

[0.21.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.19.1...v0.20.0
[0.19.1]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.19.0...v0.19.1
[0.19.0]: https://github.com/dcantor/c8000v-dmvpn-lab/compare/v0.18.0...v0.19.0
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
