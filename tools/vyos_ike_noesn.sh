#!/bin/sh
# c8000v-dmvpn-lab — a VyOS post-commit hook (/config/scripts/commit/post-hooks.d/), installed by tools/vyos_hooks.py.
#
# VyOS renders every IKE proposal twice, with and without "-noesn" (its `esn` option defaults to "disabled" and cannot
# be left out). strongSwan 6 puts a Sequence Numbers (ESN, type 5) transform on the wire for the "-noesn" variant,
# and IOS-XE 17.15 drops such an IKE_SA_INIT as malformed ("Transform type 5 invalid for protocol id 1") without
# answering — so a VyOS customer never gets an SA with the Catalyst hubs. Remove the ESN variants from the IKE
# proposals (ESP keeps them: ESN is valid there, and IOS accepts it), then reload the connections.
f=/etc/swanctl/swanctl.conf
[ -f "$f" ] || exit 0
changed=$(sudo python3 - "$f" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
def fix(m):
    items = [x for x in m.group(2).split(",") if not re.search(r"-(no)?esn$", x.strip())]
    return m.group(1) + ",".join(items)
n = re.sub(r"(?m)^(\s+proposals = )(.*)$", fix, s)
if n != s:
    open(p, "w").write(n); print("yes")
PY
)
if [ "$changed" = yes ]; then
  sudo swanctl --load-all >/dev/null 2>&1
  # a negotiation started before the fix (at boot nhrpd can be quicker than this hook) keeps retrying the old proposal
  # for ever: drop the half-open ones, nhrpd asks again with the corrected connection
  for id in $(sudo swanctl --list-sas 2>/dev/null | sed -n 's/^[^ ]*: #\([0-9]*\), CONNECTING.*/\1/p'); do
    sudo swanctl --terminate --ike-id "$id" --force >/dev/null 2>&1
  done
fi
exit 0
