#!/usr/bin/env bash
# c8000v-dmvpn-lab controller (libvirt/KVM): three Catalyst 8000v DMVPN hubs (East, Central, West), three C8000v
# customer sites, one VyOS router standing in for the MPLS provider, and an Alpine host behind every customer.
# See lab.conf.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lab.conf"

# Re-exec under the libvirt group if this login session doesn't have it yet.
if ! id -nG | tr ' ' '\n' | grep -qx libvirt && getent group libvirt | grep -qw "${USER:-$(id -un)}"; then
  exec sg libvirt -c "$(printf '%q ' "$0" "$@")"
fi

V() { virsh -q -c "$LIBVIRT_URI" "$@"; }
die() { echo "error: $*" >&2; exit 1; }
dom() { echo "$DOMAIN_PREFIX$1"; }                  # node -> libvirt domain name
node_dir() { echo "$LAB_DIR/nodes/$1"; }
known() { [[ -n "${ROLE[$1]:-}" ]] || die "unknown node: $1 (nodes: ${ALL_NODES[*]})"; }
defined() { V dominfo "$(dom "$1")" &>/dev/null; }
ours() {   # domain names are host-global: refuse to touch a same-named VM that belongs to another lab
  defined "$1" || return 0
  local xml; xml="$(V dumpxml "$(dom "$1")")"   # (no `| grep -q`: with pipefail an early grep exit makes virsh fail spuriously)
  [[ "$xml" == *"<source file='$(node_dir "$1")/"* ]] || die "a VM named $(dom "$1") exists but is not part of this lab"
}
running() { [[ "$(V domstate "$(dom "$1")" 2>/dev/null)" == "running" ]]; }
state() { V domstate "$(dom "$1")" 2>/dev/null || echo undefined; }
nodes_or_all() { local n; if [[ $# -gt 0 ]]; then for n in "$@"; do known "$n"; done; echo "$*"; else echo "${ALL_NODES[*]}"; fi; }
is_host() { [[ "${ROLE[$1]}" == "host" ]]; }
platform() { case "${ROLE[$1]}" in hub|spoke) echo "${PLATFORM[$1]:-c8000v}";; provider) echo vyos;; host) echo alpine;; esac; }
is_dmvpn() { [[ "${ROLE[$1]}" == "hub" || "${ROLE[$1]}" == "spoke" ]]; }   # carries a tunnel: hub or customer, any platform
is_c8k()  { [[ "$(platform "$1")" == "c8000v" ]]; }
is_vyos() { [[ "$(platform "$1")" == "vyos" ]]; }                          # the provider, and any VyOS customer
only() {   # only <predicate> [node..] -> the given (or all) nodes the predicate holds for
  local pred="$1" n out=(); shift
  for n in $(nodes_or_all "$@"); do "$pred" "$n" && out+=("$n"); done
  echo "${out[*]:-}"
}
# the lab's Python: the tests' virtualenv (netmiko, paramiko, robot, pyyaml), created on first use
PY="$LAB_DIR/tests/.venv/bin/python"
need_python() { [[ -x "$PY" ]] || "$LAB_DIR/tests/setup.sh"; }

# ---- networks -------------------------------------------------------------
ensure_networks() {
  if ! V net-info "$OOB_NET" &>/dev/null; then
    V net-define "$LAB_DIR/networks/$OOB_NET.xml"
    V net-autostart "$OOB_NET" >/dev/null
  fi
  [[ "$(V net-info "$OOB_NET" | awk '/Active/{print $2}')" == "yes" ]] || V net-start "$OOB_NET"
}

# ---- point-to-point links (UDP socket pairs between VMs) ------------------
port_local() { echo $(( UDP_BASE + NODE_IDX[$1]*100 + $2 )); }           # UDP port a node's NIC listens on when it anchors a link
port_far()   { echo $(( UDP_BASE + 10000 + NODE_IDX[$1]*100 + $2 )); }   # ...and the port it sends to (the other end listens there)
node_ports() { case "${ROLE[$1]}" in hub|spoke) if is_c8k "$1"; then seq 2 $((1 + C8000V_PORTS)); else seq 1 $((1 + C8000V_PORTS)); fi;; provider) seq 1 "$PROVIDER_PORTS";; host) seq 1 "$HOST_PORTS";; esac; }
port_name()  { if is_c8k "$1"; then echo "GigabitEthernet$2"; else echo "eth$2"; fi; }
mac()        { printf '%s:%02x:%02x' "$MAC_OUI" "${NODE_IDX[$1]}" "$2"; }
link_peer() {   # node port -> "peer_node peer_port prefix end(1|2)" or "" if unwired
  local me="$1:$2" l a b pfx
  for l in "${LINKS[@]}"; do
    read -r a b pfx <<<"$l"
    [[ "$a" == "$me" ]] && { echo "${b%%:*} ${b##*:} $pfx 1"; return; }
    [[ "$b" == "$me" ]] && { echo "${a%%:*} ${a##*:} $pfx 2"; return; }
  done
  return 0
}
link_ip() {     # node port -> "address/len" on that link (.1 for the first end, .2 for the second)
  local peer; peer="$(link_peer "$1" "$2")"; [[ -z "$peer" ]] && return
  read -r _ _ pfx end <<<"$peer"
  python3 -c "import ipaddress; n=ipaddress.ip_network('$pfx'); print(f'{n.network_address + $end}/{n.prefixlen}')"
}
link_addr() { link_ip "$1" "$2" | cut -d/ -f1; }

# ---- XML generation -------------------------------------------------------
serial_xml() {
  cat <<X
    <serial type='tcp'>
      <source mode='bind' host='127.0.0.1' service='${CONSOLE_PORT[$1]}'/>
      <protocol type='raw'/>
      <log file='$(node_dir "$1")/console.log' append='on'/>
      <target port='0'/>
    </serial>
X
}

no_offload_xml() {   # IOS-XE's TCP stack rejects partially-checksummed segments from the host tap
  cat <<X
      <driver name='qemu'>
        <host csum='off' gso='off' tso4='off' tso6='off' ecn='off' ufo='off'/>
        <guest csum='off' tso4='off' tso6='off' ecn='off' ufo='off'/>
      </driver>
X
}

udp_nic_xml() {    # node port slot -> one <interface type='udp'> (the first end of a link listens on its own port_local; the second mirrors it)
  local n="$1" p="$2" slot="$3" peer remote local pn pp pfx end
  peer="$(link_peer "$n" "$p")"; local="$(port_local "$n" "$p")"; remote="$(port_far "$n" "$p")"
  if [[ -n "$peer" ]]; then
    read -r pn pp pfx end <<<"$peer"
    [[ "$end" == "2" ]] && { local="$(port_far "$pn" "$pp")"; remote="$(port_local "$pn" "$pp")"; }
    echo "    <!-- $(port_name "$n" "$p"): $(link_ip "$n" "$p") <-> $pn $(port_name "$pn" "$pp") ($pfx) -->"
  else
    echo "    <!-- $(port_name "$n" "$p"): unwired -->"
  fi
  cat <<X
    <interface type='udp'>
      <mac address='$(mac "$n" "$p")'/>
      <source address='127.0.0.1' port='$remote'>
        <local address='127.0.0.1' port='$local'/>
      </source>
      <model type='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='$(printf '0x%02x' "$slot")' function='0x0'/>
    </interface>
X
}

domain_head_xml() {   # name, deterministic UUID (from the mgmt IP), memory, cpu
  local n="$1" title="$2" ram="$3" vcpu="$4"
  cat <<X
<domain type='kvm'>
  <name>$(dom "$n")</name>
  <uuid>$(uuidgen --sha1 --namespace @dns --name "$LAB_NAME.${MGMT_IP[$n]}")</uuid>
  <title>$title</title>
  <memory unit='MiB'>$ram</memory>
  <vcpu placement='static'>$vcpu</vcpu>
  <cpu mode='host-passthrough' check='none'/>
  <os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
X
}

domain_tail_xml() {
  serial_xml "$1"
  cat <<X
    <memballoon model='none'/>
  </devices>
</domain>
X
}

oob_nic_xml() {    # node port(0|1) [offload] -> the NIC on the OOB libvirt network, always PCI slot 3
  cat <<X
    <!-- $(port_name "$1" "$2"): OOB management ${MGMT_IP[$1]} -->
    <interface type='network'>
      <mac address='$(mac "$1" "$2")'/>
      <source network='$OOB_NET'/>
      <model type='virtio'/>
$([[ "${3:-}" == "no-offload" ]] && no_offload_xml)
      <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
    </interface>
X
}

c8k_xml() {        # C8000v: IDE disk + day-0 ISO; Gi1 = OOB (Mgmt-vrf), Gi2.. = point-to-point links (black-holed when unwired)
  local n="$1" d p; d="$(node_dir "$n")"
  domain_head_xml "$n" "Catalyst 8000v ${ROLE[$n]} ($n, ${REGION[$n]:-lab})" "$C8000V_RAM_MIB" "$C8000V_VCPU"
  cat <<X
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$d/disk.qcow2'/>
      <target dev='hda' bus='ide'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$d/config.iso'/>
      <target dev='hdc' bus='ide'/>
      <readonly/>
    </disk>
X
  oob_nic_xml "$n" 1 no-offload
  for p in $(node_ports "$n"); do udp_nic_xml "$n" "$p" $((2 + p)); done   # GigabitEthernetN in PCI slot N+2
  domain_tail_xml "$n"
}

vyos_xml() {       # VyOS (the provider, or a customer): virtio disk, eth0 = OOB, eth1.. = its links
  local n="$1" d p; d="$(node_dir "$n")"
  domain_head_xml "$n" "VyOS $([[ "${ROLE[$n]}" == provider ]] && echo "MPLS provider" || echo "${ROLE[$n]}") ($n${REGION[$n]:+, ${REGION[$n]}})" "$VYOS_RAM_MIB" "$VYOS_VCPU"
  cat <<X
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$d/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
X
  oob_nic_xml "$n" 0
  for p in $(node_ports "$n"); do udp_nic_xml "$n" "$p" $((3 + p)); done
  domain_tail_xml "$n"
}

host_xml() {       # Alpine LAN host: eth0 = OOB, eth1 = UDP link to its customer router's Gi3; cloud-init seed on a cdrom
  local n="$1" d; d="$(node_dir "$n")"
  domain_head_xml "$n" "Alpine LAN host ($n)" "$HOST_RAM_MIB" "$HOST_VCPU"
  cat <<X
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$d/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$d/seed.iso'/>
      <target dev='hda' bus='ide'/>
      <readonly/>
    </disk>
X
  oob_nic_xml "$n" 0
  udp_nic_xml "$n" 1 4
  domain_tail_xml "$n"
}

# ---- build ------------------------------------------------------------------
overlay_disk() {   # node base-image
  local d; d="$(node_dir "$1")"; mkdir -p "$d"
  [[ -f "$2" ]] || die "base image not found: $2"
  if [[ ! -f "$d/disk.qcow2" ]]; then
    echo "[$1] creating overlay disk on $(basename "$2")"
    qemu-img create -q -f qcow2 -b "$2" -F qcow2 "$d/disk.qcow2"
  fi
}

gen_configs() { python3 "$LAB_DIR/tools/gen_configs.py" | sed 's/^/[render] /'; }

build_c8k() {
  local n="$1" d; d="$(node_dir "$n")"
  [[ -f "$d/iosxe_config.txt" ]] || die "$d/iosxe_config.txt missing — run tools/gen_configs.py"
  overlay_disk "$n" "$C8000V_IMAGE"
  # temp file + rename: libvirt chowns the previous ISO to libvirt-qemu
  genisoimage -quiet -o "$d/config.iso.tmp" -l -J -r -V config "$d/iosxe_config.txt" && mv -f "$d/config.iso.tmp" "$d/config.iso"
  c8k_xml "$n" > "$d/domain.xml"
  V define "$d/domain.xml" >/dev/null
}

build_vyos() {
  local n="$1" d; d="$(node_dir "$n")"
  [[ -f "$d/vyos_config.txt" ]] || die "$d/vyos_config.txt missing — run tools/gen_configs.py"
  overlay_disk "$n" "$VYOS_IMAGE"
  vyos_xml "$n" > "$d/domain.xml"
  V define "$d/domain.xml" >/dev/null
}

host_seed() {   # cloud-init NoCloud seed for an Alpine LAN host: static addresses (network-config v2 by MAC), lab / lab, sshd
  local n="$1" d peer pn pp pfx cidr gw; d="$(node_dir "$n")"
  peer="$(link_peer "$n" 1)"; [[ -n "$peer" ]] || die "$n eth1 is not wired in LINKS"
  read -r pn pp pfx _ <<<"$peer"
  cidr="$(link_ip "$n" 1)"; gw="$(link_addr "$pn" "$pp")"
  echo "[$n] building cloud-init (NoCloud) seed ISO"
  cat > "$d/network-config" <<U
version: 2
ethernets:
  oob:
    match: { macaddress: "$(mac "$n" 0)" }
    set-name: eth0
    addresses: [${MGMT_IP[$n]}/24]
    routes: [{ to: 10.0.0.0/8, via: $OOB_GATEWAY }]
  lan:
    match: { macaddress: "$(mac "$n" 1)" }
    set-name: eth1
    addresses: [$cidr]
    # the site LANs, and the overlay: a customer answers a traceroute from its tunnel address, and without a route
    # back to it the host's reverse-path filter drops the reply
    routes: [{ to: 192.168.0.0/16, via: $gw }, { to: $DMVPN_OVERLAY, via: $gw }]
U
  cat > "$d/user-data" <<U
#cloud-config
# $n: eth0 = OOB management (${MGMT_IP[$n]}), eth1 = $pn $(port_name "$pn" "$pp") (site LAN $pfx, gateway $gw)
hostname: $n
users:
  - name: lab
    plain_text_passwd: lab
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
ssh_pwauth: true
write_files:
  - path: /etc/motd
    content: "$n — the LAN host behind $pn: eth1 $cidr (gateway $gw), OOB eth0 ${MGMT_IP[$n]}. iperf3 / tcpdump / mtr installed.\n"
runcmd:
  - rc-update add sshd default
  - rc-service sshd restart
  - rc-update add node-exporter default
  - rc-service node-exporter restart
U
  # a new instance-id whenever the seed changes, so cloud-init re-applies it on the next boot
  printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$n" "$(cat "$d/network-config" "$d/user-data" | md5sum | cut -c1-8)" "$n" > "$d/meta-data"
  genisoimage -quiet -o "$d/seed.iso.tmp" -V cidata -J -r "$d/user-data" "$d/meta-data" "$d/network-config" && mv -f "$d/seed.iso.tmp" "$d/seed.iso"
}

build_host() {
  local n="$1" d; d="$(node_dir "$n")"
  overlay_disk "$n" "$HOST_IMAGE"
  host_seed "$n"
  host_xml "$n" > "$d/domain.xml"
  V define "$d/domain.xml" >/dev/null
}

build() { if is_c8k "$1"; then build_c8k "$1"; elif is_vyos "$1"; then build_vyos "$1"; else build_host "$1"; fi; }

# ---- readiness / day-0 ----------------------------------------------------
ssh_ready() { timeout 8 bash -c "exec 3<>/dev/tcp/${MGMT_IP[$1]}/22" 2>/dev/null; }
restconf_ready() { [[ "$(curl -sk -u "${IOSXE_USERNAME:-admin}:${IOSXE_PASSWORD:-admin}" -m 8 -o /dev/null -w '%{http_code}' \
                     "https://${MGMT_IP[$1]}/restconf/data/Cisco-IOS-XE-native:native/hostname" -H 'Accept: application/yang-data+json' 2>/dev/null)" == "200" ]]; }
ready() { if is_c8k "$1"; then restconf_ready "$1"; else ssh_ready "$1"; fi; }

save_config() {   # write memory: RESTCONF RPC first, serial console as fallback
  local n="$1" user="${IOSXE_USERNAME:-admin}" pass="${IOSXE_PASSWORD:-admin}"
  curl -sk -u "$user:$pass" -m 30 -X POST "https://${MGMT_IP[$n]}/restconf/operations/cisco-ia:save-config" \
       -H 'Content-Type: application/yang-data+json' -H 'Accept: application/yang-data+json' 2>/dev/null | grep -qi success && return 0
  timeout 90 python3 "$LAB_DIR/tools/console.py" send 127.0.0.1 "${CONSOLE_PORT[$n]}" "write memory" >/dev/null 2>&1
}

bootstrap_vyos() {   # VyOS day-0 over the serial console (nodes/<n>/vyos_config.txt), logged to nodes/<n>/bootstrap.log
  local n="$1" d; d="$(node_dir "$n")"
  {
    echo "[$n] waiting for the VyOS login prompt..."
    python3 "$LAB_DIR/tools/vyos_console.py" wait 127.0.0.1 "${CONSOLE_PORT[$n]}" 600
    echo "[$n] applying day-0 config"
    python3 "$LAB_DIR/tools/vyos_console.py" push 127.0.0.1 "${CONSOLE_PORT[$n]}" "$d/vyos_config.txt"
    for _ in $(seq 30); do ssh_ready "$n" && break; sleep 5; done
    ssh_ready "$n" && echo "[$n] ready: ssh vyos@${MGMT_IP[$n]} (vyos)" || echo "[$n] warning: SSH not answering yet"
    # a VyOS customer talks IKEv2 to the Catalyst hubs: install the post-commit hook that keeps its IKE proposal IOS-compatible
    if [[ "${ROLE[$n]}" == spoke ]]; then "$PY" "$LAB_DIR/tools/vyos_hooks.py" "${MGMT_IP[$n]}" || echo "[$n] warning: hooks not installed"; fi
  } > "$d/bootstrap.log" 2>&1
}

bootstrap_c8k() {    # C8000v day-0 over the serial console, license reload if needed, then wait for RESTCONF
  local n="$1" d c; d="$(node_dir "$n")"; c="${CONSOLE_PORT[$n]}"
  {
    echo "[$n] waiting for the console prompt (C8000v takes ~3-5 min on first boot)..."
    python3 "$LAB_DIR/tools/console.py" wait 127.0.0.1 "$c" 1800
    echo "[$n] applying day-0 config + generating SSH keys"
    python3 "$LAB_DIR/tools/console.py" push 127.0.0.1 "$c" "$d/iosxe_config.txt"
    python3 "$LAB_DIR/tools/console.py" push 127.0.0.1 "$c" "$LAB_DIR/nodes/_template/post-boot.txt"
    # the crypto feature set needs the license boot level, which only takes effect after a reload
    if ! python3 "$LAB_DIR/tools/console.py" send 127.0.0.1 "$c" "show version | include ^License Level" 2>/dev/null | grep -q 'network-advantage'; then
      echo "[$n] license boot level not active yet: reloading once"
      python3 "$LAB_DIR/tools/console.py" reload 127.0.0.1 "$c"
      sleep 60
      python3 "$LAB_DIR/tools/console.py" wait 127.0.0.1 "$c" 1800
    fi
    local i; for i in $(seq 60); do restconf_ready "$n" && break; sleep 10; done
    restconf_ready "$n" && echo "[$n] ready: ssh admin@${MGMT_IP[$n]} (admin), RESTCONF up" || echo "[$n] warning: RESTCONF not answering yet"
  } > "$d/bootstrap.log" 2>&1
}

# ---- commands ---------------------------------------------------------------
start_node() {
  local n="$1" log; log="$(node_dir "$n")/console.log"
  ours "$n"; defined "$n" || build "$n"
  # pre-create the console log so virtlogd appends to our file instead of a root-only one
  [[ -f "$log" ]] || { touch "$log"; chmod 644 "$log"; }
  if running "$n"; then echo "[$n] already running"; return 1; fi
  V start "$(dom "$n")" >/dev/null; echo "[$n] started as $(dom "$n") (console: 127.0.0.1:${CONSOLE_PORT[$n]})"
}

cmd_up() {         # the provider and hosts first, then the C8000vs BOOT_BATCH at a time
  ensure_networks; gen_configs
  local n started=0
  for n in $(nodes_or_all "$@"); do is_c8k "$n" || start_node "$n" || true; done
  for n in $(only is_c8k "$@"); do
    if (( started > 0 && started % BOOT_BATCH == 0 )); then echo "   ...waiting ${BOOT_STAGGER_S}s before the next C8000v batch"; sleep "$BOOT_STAGGER_S"; fi
    start_node "$n" && started=$((started + 1)) || true
  done
}

cmd_down() {       # C8000v: save the config, then power off; VyOS / hosts: ACPI shutdown
  local n i
  for n in $(nodes_or_all "$@"); do
    running "$n" || { echo "[$n] not running"; continue; }
    ours "$n"
    if is_c8k "$n"; then
      echo "[$n] saving config, then powering off"
      save_config "$n" || echo "[$n] warning: could not save config"
      V destroy "$(dom "$n")" >/dev/null
    else
      V shutdown "$(dom "$n")" >/dev/null
      for i in $(seq 30); do running "$n" || break; sleep 2; done
      running "$n" && V destroy "$(dom "$n")" >/dev/null
    fi
    echo "[$n] stopped"
  done
}

cmd_bootstrap() {  # day-0 over the serial consoles, every node in parallel; each logs to nodes/<n>/bootstrap.log
  local n pids=() rc=0 p log
  need_python
  for n in $(nodes_or_all "$@"); do
    if is_c8k "$n"; then bootstrap_c8k "$n" & pids+=($!)
    elif is_vyos "$n"; then bootstrap_vyos "$n" & pids+=($!); fi
  done
  echo "bootstrapping in parallel — follow along with: tail -f nodes/*/bootstrap.log"
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  for n in $(nodes_or_all "$@"); do
    if is_host "$n"; then
      for _ in $(seq 60); do ssh_ready "$n" && break; sleep 5; done
      ssh_ready "$n" && echo "[$n] ready" || { echo "[$n] SSH NOT ready"; rc=1; }
      continue
    fi
    # judge by the helpers' own verdict lines: an IOS console is full of words like "exec-timeout" and "Warning!!!"
    log="$(node_dir "$n")/bootstrap.log"
    if grep -qE "^\[$n\] ready: " "$log" 2>/dev/null && ! grep -qE "^!! |^Traceback|Commit failed|^\[$n\] warning:" "$log"
      then echo "[$n] day-0 applied"
      else echo "[$n] day-0 PROBLEM — see $log"; rc=1; fi
  done
  return $rc
}

cmd_configure() {  # (re)apply the rendered provider config over SSH — idempotent (the C8000vs are configured by `nac`)
  need_python; gen_configs
  local n
  for n in $(only is_vyos "$@"); do
    "$PY" "$LAB_DIR/tools/vyos_push.py" "${MGMT_IP[$n]}" "$(node_dir "$n")/vyos_config.txt" | sed "s/^/[$n] /"
    if [[ "${ROLE[$n]}" == spoke ]]; then "$PY" "$LAB_DIR/tools/vyos_hooks.py" "${MGMT_IP[$n]}" | sed "s/^/[$n] /"; fi
  done
}

cmd_wait() {       # block until RESTCONF (C8000v) / SSH (VyOS, hosts) answers
  local n i
  for n in $(nodes_or_all "$@"); do
    for i in $(seq 120); do ready "$n" && break; sleep 10; done
    ready "$n" && echo "[$n] ready" || { echo "[$n] NOT ready"; return 1; }
  done
}

cmd_rebuild() {    # re-define domains (and day-0 ISOs / host seeds) from lab.conf without touching disks
  local n; gen_configs
  for n in $(nodes_or_all "$@"); do
    ours "$n"; running "$n" && die "$n is running — ./lab.sh down $n first"
    defined "$n" && V undefine "$(dom "$n")" >/dev/null
    build "$n"; echo "[$n] redefined"
  done
}

cmd_clean() {      # destroy VMs and delete overlay disks (the base images are untouched)
  local n d
  for n in $(nodes_or_all "$@"); do
    ours "$n"; running "$n" && V destroy "$(dom "$n")" >/dev/null
    defined "$n" && V undefine "$(dom "$n")" >/dev/null
    d="$(node_dir "$n")"
    rm -f "$d"/{disk.qcow2,config.iso,seed.iso,meta-data,user-data,network-config,domain.xml,console.log,bootstrap.log}
    echo "[$n] cleaned"
  done
}

cmd_status() {
  # NODE ROLE STATE first: the lab hub (lab-portal) reads those three columns to count this lab's VMs
  printf '%-12s %-9s %-10s %-15s %-13s %-12s %-16s %-7s\n' NODE ROLE STATE VM MGMT-IP NBMA TUNNEL0 CONSOLE
  local n t
  for n in "${ALL_NODES[@]}"; do
    t="${T_IDX[$n]:-}"
    printf '%-12s %-9s %-10s %-15s %-13s %-12s %-16s %-7s\n' "$n" "${ROLE[$n]}" "$(state "$n")" "$(dom "$n")" "${MGMT_IP[$n]}" \
      "${t:+$WAN_NET.$t.2}" "${t:+${DMVPN_OVERLAY%.*}.$t}" "${CONSOLE_PORT[$n]}"
  done
  echo; echo "DMVPN: one phase 3 cloud, Tunnel0 $DMVPN_OVERLAY, network-id $DMVPN_NETWORK_ID, iBGP AS $DMVPN_AS; provider AS $PROVIDER_AS"
  echo "links:"
  local l a b pfx
  for l in "${LINKS[@]}"; do read -r a b pfx <<<"$l"
    printf '  %-26s %-17s <-> %-26s %s\n' "${a%%:*} $(port_name "${a%%:*}" "${a##*:}")" "$(link_ip "${a%%:*}" "${a##*:}")" \
      "${b%%:*} $(port_name "${b%%:*}" "${b##*:}")" "$(link_ip "${b%%:*}" "${b##*:}")"
  done
}

cmd_inventory() {  # the lab as JSON — the one contract the renderer, the tests, Nautobot and the portal all read
  local var k n p
  {
    for var in LAB_NAME DOMAIN_PREFIX OOB_NET OOB_GATEWAY OOB_PREFIX NMS_IP DMVPN_AS DMVPN_OVERLAY DMVPN_NETWORK_ID DMVPN_TUNNEL_KEY \
               DMVPN_HOLDTIME DMVPN_MTU DMVPN_MSS DMVPN_PSK NHRP_SECRET BGP_KEEPALIVE BGP_HOLDTIME PROVIDER_AS WAN_NET \
               ROUTER_ID_NET LAN_PORT MAC_OUI; do
      printf 'scalar\t%s\t%s\n' "$var" "${!var}"
    done
    for n in "${ALL_NODES[@]}"; do printf 'map\tPLATFORM\t%s\t%s\n' "$n" "$(platform "$n")"; done
    for var in ROLE REGION MGMT_IP T_IDX LAN HOST_OF CONSOLE_PORT NODE_IDX; do
      declare -n A="$var"
      for k in "${!A[@]}"; do printf 'map\t%s\t%s\t%s\n' "$var" "$k" "${A[$k]}"; done
      unset -n A
    done
    for var in HUBS SPOKES PROVIDERS HOSTS ALL_NODES; do
      declare -n A="$var"; printf 'list\t%s\t%s\n' "$var" "${A[*]}"; unset -n A
    done
    for n in "${ALL_NODES[@]}"; do
      for p in $(node_ports "$n"); do printf 'port\t%s\t%s\t%s\t%s\n' "$n" "$p" "$(port_name "$n" "$p")" "$(link_peer "$n" "$p")"; done
    done
  } | python3 "$LAB_DIR/tools/inventory.py"
}

cmd_console() {
  local n="${1:?node}"; known "$n"; running "$n" || die "$n is not running"
  echo "attaching to $n (exit: Ctrl-] then q)"; socat -,raw,echo=0,escape=0x1d "tcp:127.0.0.1:${CONSOLE_PORT[$n]}"
}

cmd_ssh() {
  local n="${1:?node}"; shift || true; known "$n"
  local u=admin opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
  is_vyos "$n" && u=vyos
  if is_host "$n"; then u=lab; opts+=(-o PubkeyAuthentication=no); echo "(host: user lab, password lab)" >&2; fi
  ssh "${opts[@]}" "$u@${MGMT_IP[$n]}" "$@"
}

cmd_log() { known "${1:?node}"; tail -n "${2:-50}" -f "$(node_dir "$1")/console.log"; }

cmd_nac() {        # terraform in nac/ with the router credentials in the environment; an apply ends in write memory
  export PATH="$HOME/.local/bin:$PATH"
  command -v terraform >/dev/null || die "terraform not found in PATH"
  gen_configs
  local rc=0 n
  ( cd "$LAB_DIR/nac" && IOSXE_USERNAME="${IOSXE_USERNAME:-admin}" IOSXE_PASSWORD="${IOSXE_PASSWORD:-admin}" terraform "$@" ) || rc=$?
  if [[ $rc -eq 0 && "${1:-}" == "apply" ]]; then
    for n in $(only is_c8k "${DMVPN_ROUTERS[@]}"); do save_config "$n" && echo "[$n] running-config saved" || echo "[$n] warning: save failed" >&2; done
  fi
  return $rc
}

ios() { need_python; "$PY" "$LAB_DIR/tools/ios_cmd.py" "$@"; }
vy()  { need_python; "$PY" "$LAB_DIR/tools/vyos_cmd.py" "${MGMT_IP[$1]}" "${@:2}"; }

cmd_verify() {     # a quick look at every layer, bottom up
  local n
  echo "== provider: eBGP sessions on mpls =="; vy mpls "show ip bgp summary" | sed 's/^/  /'
  echo; echo "== each router: underlay eBGP, NHRP, IPsec, overlay iBGP =="
  for n in "${DMVPN_ROUTERS[@]}"; do
    echo "[$n] ($(platform "$n"))"
    if is_c8k "$n"; then ios "$n" "show ip bgp summary | begin Neighbor" "show dmvpn | begin Peer" "show crypto session brief" | sed 's/^/  /'
    else vy "$n" "sudo vtysh -c 'show ip bgp summary'" "sudo vtysh -c 'show ip nhrp nhs'" "show vpn ipsec sa" | sed 's/^/  /'; fi
  done
  echo; echo "== LAN to LAN =="; need_python; "$PY" "$LAB_DIR/tools/host_cmd.py" matrix
}

# ---- Nautobot (the shared NMS of the cat9000v lab, http://10.0.0.10:8080) -------------------------------------------
NAUTOBOT_URL="${NAUTOBOT_URL:-http://10.0.0.10:8080}"
nautobot_token() { [[ -n "${NAUTOBOT_TOKEN:-}" ]] && { echo "$NAUTOBOT_TOKEN"; return; }
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR lab@10.0.0.10 \
      'grep ^NAUTOBOT_SUPERUSER_API_TOKEN /opt/nautobot/.env | cut -d= -f2'; }
cmd_nautobot() {   # seed [--check] | render [--check|--write|--inventory] | token
  need_python
  local sub="${1:?seed|render|token}"; shift || true
  case "$sub" in
    token) nautobot_token ;;
    seed|render) NAUTOBOT_URL="$NAUTOBOT_URL" NAUTOBOT_TOKEN="$(nautobot_token)" "$PY" "$LAB_DIR/nautobot/$sub.py" "$@" ;;
    remove-customer) NAUTOBOT_URL="$NAUTOBOT_URL" NAUTOBOT_TOKEN="$(nautobot_token)" "$PY" "$LAB_DIR/nautobot/remove_customer.py" \
                       --domain-prefix "$DOMAIN_PREFIX" "$@" ;;
    *) die "unknown nautobot subcommand: $sub (seed | render | remove-customer | token)" ;;
  esac
}

cmd_hosts() {      # `hosts` = ping matrix between the LAN hosts, `hosts run NAME CMD`
  need_python; "$PY" "$LAB_DIR/tools/host_cmd.py" "${1:-matrix}" "${@:2}"
}

cmd_webapp() {     # the DMVPN provisioning portal (FastAPI/uvicorn) on http://<host>:8094
  [[ -x "$LAB_DIR/webapp/.venv/bin/uvicorn" ]] || "$LAB_DIR/webapp/setup.sh"
  export PATH="$HOME/.local/bin:$PATH"
  cd "$LAB_DIR/webapp" && exec .venv/bin/uvicorn app:app --host "${WEBAPP_HOST:-0.0.0.0}" --port "${WEBAPP_PORT:-8094}" "$@"
}

cmd_test() {       # Robot Framework suites; results in results/<date>_<time>/
  need_python; exec "$LAB_DIR/tests/run.sh" "$@"
}

usage() {
  cat <<U
usage: ./lab.sh <command> [node ...]

  up [node..]        define (if needed) and start VMs; C8000vs $BOOT_BATCH at a time
  bootstrap [node..] day-0 over the serial consoles (first boot: C8000v license reload, ~8 min)
  wait [node..]      wait until RESTCONF (C8000v) / SSH (VyOS, hosts) answers
  nac <tf args..>    terraform in nac/ (init | plan | apply) — the C8000v configuration
  configure [node..] re-apply the rendered provider config over SSH (idempotent)
  verify             provider, underlay, NHRP, IPsec, iBGP, host ping matrix
  hosts [run N CMD]  ping matrix between the LAN hosts (or run a command on one)
  test [robot args]  run the Robot Framework suites -> results/<date>_<time>/
  nautobot <sub>     seed [--check] | render [--check|--write|--inventory] | remove-customer NAME .. | token
  webapp             serve the provisioning portal on :8094
  status             nodes, VMs, addresses, links
  inventory          the lab as JSON — the contract every tool reads
  down [node..]      save (C8000v) and stop VMs
  console <node>     attach to the serial console (exit: Ctrl-] then q)
  ssh <node> [cmd]   ssh over OOB (C8000v admin/admin, VyOS vyos/vyos, hosts lab/lab)
  log <node> [n]     follow a node's console log
  rebuild [node..]   re-generate domain XML / day-0 ISO / seeds (keeps disks)
  clean [node..]     destroy VMs and delete overlay disks

nodes: ${ALL_NODES[*]}   (libvirt: ${DOMAIN_PREFIX}<node>)
U
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up|down|bootstrap|configure|wait|status|inventory|console|ssh|log|nac|verify|hosts|test|nautobot|webapp|rebuild|clean) "cmd_$cmd" "$@" ;;
  *) usage; [[ -z "$cmd" || "$cmd" == help || "$cmd" == -h ]] && exit 0; exit 1 ;;
esac
