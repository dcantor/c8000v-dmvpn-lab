# Changelog

All notable changes to c8000v-dmvpn-lab are recorded here, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **MAJOR:** a change that breaks how the lab is used, for example `lab.conf` keys, `lab.sh` commands or the portal API.
- **MINOR:** a new capability.
- **PATCH:** a fix to something that already existed.

While the version is 0.x, the lab's interfaces are still settling. The current version is in [`VERSION`](VERSION), in
the portal's header, and in git as a `v<version>` tag.

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
