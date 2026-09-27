#!/usr/bin/env bash
# Run the Robot Framework suites against the lab.
#   tests/run.sh [robot options...]      e.g.  tests/run.sh suites/03_dmvpn.robot   or   tests/run.sh --exclude nautobot
# Every run gets its own folder: results/YYYY-MM-DD_HH-MM-SS/
#   configs/pre-run/, configs/post-run/   running + startup config of every router before and after the tests
#   configs/pre-vs-post.diff              what the run changed (empty = nothing)
#   log.html, report.html, output.xml     Robot Framework results
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -x .venv/bin/robot ]] || { echo "error: run tests/setup.sh first" >&2; exit 1; }

ts="$(date +%Y-%m-%d_%H-%M-%S)"
out="$(cd .. && pwd)/results/$ts"
mkdir -p "$out/configs"
echo "==> results: $out"

echo "==> capturing router configurations (pre-run)"
.venv/bin/python capture_configs.py "$out/configs/pre-run" || echo "warning: config capture failed" >&2

# a suite path given on the command line replaces the default of every suite
suites=(suites/); for a in "$@"; do [[ "$a" == suites/* ]] && suites=(); done
# the report's title is the lab's name; which router runs what is spelled out in its metadata
routers="$(../lab.sh inventory | python3 -c '
import json, sys
d = json.load(sys.stdin); P = {"c8000v": "Catalyst 8000v", "vyos": "VyOS"}
print(" · ".join(n["name"] + ": " + P.get(n["platform"], n["platform"]) for n in d["nodes"] if n["role"] in ("hub", "spoke", "provider")))')"
echo "==> running Robot Framework suites"
.venv/bin/robot --outputdir "$out" --name "c8000v-dmvpn-lab" --loglevel INFO --pythonpath resources \
  --metadata "Lab:c8000v-dmvpn-lab (the lab's name — not a router type)" --metadata "Routers:$routers" "$@" "${suites[@]}"
rc=$?

echo "==> capturing router configurations (post-run backup)"
.venv/bin/python capture_configs.py "$out/configs/post-run" || echo "warning: config capture failed" >&2
diff -ru -I '^[!#] .* captured ' "$out/configs/pre-run" "$out/configs/post-run" > "$out/configs/pre-vs-post.diff" \
  && echo "    no configuration changes during the run" \
  || echo "    configuration changed during the run, see configs/pre-vs-post.diff"

ln -sfn "$(basename "$out")" ../results/latest
echo "==> report: $out/report.html  (rc=$rc)"
exit $rc
