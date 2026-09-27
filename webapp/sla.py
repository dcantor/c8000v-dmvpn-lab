"""Per-customer service levels: latency, loss and tunnel uptime, measured continuously and kept in VictoriaMetrics.

Measuring. Every minute (while the lab runs) the portal logs in to each customer's LAN host and pings every hub's LAN —
where the applications are served — five times, in parallel: the round trip crosses the customer router, the DMVPN
and the hub, the path the customer's traffic takes. The results are exported on /metrics (lab_sla_rtt_ms,
lab_sla_loss_ratio); Prometheus on the NMS scrapes them and writes them to VictoriaMetrics, 180 days.

Tunnel uptime needs no new probe: the portal has exported lab_dmvpn_nhs_up / lab_dmvpn_nhs_expected per customer every
scrape since 0.5.0 — how many hubs it is registered with, and how many it should be.

Reporting. `report()` asks VictoriaMetrics for a window (the last 24 h / 7 d / 30 d, or a calendar month): availability
(registered with at least one hub), full registration (all hubs), latency average and 95th percentile and loss per hub,
how much of the window was monitored at all, and time series for the charts. Targets are the lab's own — a lab SLA."""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from state import HOST_PASS, HOST_USER, _ssh

TARGETS = {"availability": 99.9, "rtt_ms": 20.0, "loss_pct": 0.5}
LINE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?(?:= [\d.]+/([\d.]+)/([\d.]+) ms)?\s*@(\S+)")


class Prober:
    def __init__(self):
        self.latest, self.lock = {}, threading.Lock()   # customer -> hub -> {rtt_avg, rtt_max, loss, t}

    def probe_host(self, customer, host, hubs):
        """hubs: {hub: lan gateway}. One SSH login, five pings to every hub at once."""
        cmd = " ".join(f'(ping -c 5 -i 0.2 -W 1 -q {ip} 2>&1 | tail -2 | tr "\\n" " "; echo "@{ip}") &' for ip in hubs.values()) + " wait"
        out = _ssh(host, cmd, HOST_USER, HOST_PASS, timeout=30)
        by_ip = {ip: h for h, ip in hubs.items()}
        res = {}
        for m in LINE.finditer(out):
            sent, recv, avg, mx, ip = int(m[1]), int(m[2]), m[3], m[4], m[5]
            if ip in by_ip:
                res[by_ip[ip]] = {"loss": (sent - recv) / sent if sent else 1.0, "rtt_avg": float(avg) if avg else None,
                                  "rtt_max": float(mx) if mx else None, "t": time.time()}
        for h in hubs:                                 # a hub that printed nothing was not reached at all
            res.setdefault(h, {"loss": 1.0, "rtt_avg": None, "rtt_max": None, "t": time.time()})
        return res

    def run_once(self, inv):
        nodes = {n["name"]: n for n in inv["nodes"]}
        hubs = {h: str(__import__("ipaddress").ip_network(nodes[h]["lan"]).network_address + 1) for h in inv["service"]["hubs"]}
        custs = [c for c in inv["service"]["spokes"] if nodes[c].get("host") in nodes]

        def one(c):
            try:
                return c, self.probe_host(c, nodes[nodes[c]["host"]]["mgmt_ip"], hubs)
            except Exception:                                   # noqa: BLE001 — an unreachable host is total loss
                return c, {h: {"loss": 1.0, "rtt_avg": None, "rtt_max": None, "t": time.time(), "error": True} for h in hubs}

        with ThreadPoolExecutor(max_workers=8) as ex:
            got = dict(ex.map(one, custs))
        with self.lock:
            self.latest = got

    def metrics(self, lab, metric_line):
        out = ["# HELP lab_sla_rtt_ms Round trip from the customer's LAN host to the hub's LAN (average of 5 pings)",
               "# TYPE lab_sla_rtt_ms gauge",
               "# HELP lab_sla_loss_ratio Share of the 5 pings from the customer's LAN host to the hub's LAN that were lost",
               "# TYPE lab_sla_loss_ratio gauge"]
        with self.lock:
            snap = dict(self.latest)
        for c, per in sorted(snap.items()):
            for h, r in sorted(per.items()):
                lab_ = {"lab": lab, "customer": c, "hub": h}
                if r["rtt_avg"] is not None:
                    out.append(metric_line("lab_sla_rtt_ms", lab_, r["rtt_avg"]))
                out.append(metric_line("lab_sla_loss_ratio", lab_, round(r["loss"], 3)))
        return out


# ---- reports from VictoriaMetrics ------------------------------------------------------------------------------------
def window_bounds(window):
    """'24h' | '7d' | '30d' | 'month' (this calendar month so far) | 'YYYY-MM' -> (start, end, label)."""
    now = time.time()
    if window in ("24h", "7d", "30d"):
        secs = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}[window]
        return now - secs, now, {"24h": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days"}[window]
    tz = datetime.now().astimezone().tzinfo            # months are the lab host's calendar months
    if window == "month":
        d = datetime.now(tz)
        start = datetime(d.year, d.month, 1, tzinfo=tz).timestamp()
        return start, now, d.strftime("%B %Y") + " (month to date)"
    m = re.fullmatch(r"(\d{4})-(\d{2})", window or "")
    if m:
        y, mo = int(m[1]), int(m[2])
        start = datetime(y, mo, 1, tzinfo=tz).timestamp()
        end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=tz).timestamp()
        if start > now:
            raise ValueError(f"{window} has not begun")
        return start, min(end, now), datetime(y, mo, 1).strftime("%B %Y")
    raise ValueError("window is one of 24h, 7d, 30d, month, YYYY-MM")


class VM:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def q(self, expr, at):
        r = requests.get(f"{self.url}/api/v1/query", params={"query": expr, "time": int(at)}, timeout=30)
        r.raise_for_status()
        return r.json()["data"]["result"]

    def qr(self, expr, start, end, step):
        # points fall on multiples of the step: one more step makes sure the last one covers the newest samples
        r = requests.get(f"{self.url}/api/v1/query_range", params={"query": expr, "start": int(start), "end": int(end + step), "step": f"{int(step)}s"},
                         timeout=30)
        r.raise_for_status()
        return r.json()["data"]["result"]


def report(vm_url, lab, customer, hubs, window="30d"):
    start, end, label = window_bounds(window)
    span = max(int(end - start), 3600)
    rng = f"{span}s"
    vm = VM(vm_url)
    sel = f'lab="{lab}",router="{customer}"'
    one = lambda res: float(res[0]["value"][1]) if res else None   # noqa: E731
    reg_up = f'(lab_dmvpn_nhs_up{{{sel}}} > bool 0)'
    reg_full = f'(lab_dmvpn_nhs_up{{{sel}}} >= bool on(router) lab_dmvpn_nhs_expected{{{sel}}})'
    step = max(60, span // 240)
    out = {"customer": customer, "window": window, "label": label, "start": start, "end": end, "targets": TARGETS, "hubs": {},
           "series": {}}
    samples = one(vm.q(f"sum(count_over_time(lab_dmvpn_nhs_up{{{sel}}}[{rng}]))", end))
    scrape = 30.0                                  # the NMS scrapes the portal every 30 s
    out["coverage"] = min(1.0, (samples or 0) * scrape / span)
    out["availability"] = one(vm.q(f"avg_over_time({reg_up}[{rng}:1m])", end))
    out["full_registration"] = one(vm.q(f"avg_over_time({reg_full}[{rng}:1m])", end))
    rtt_samples = one(vm.q(f'sum(count_over_time(lab_sla_rtt_ms{{lab="{lab}",customer="{customer}"}}[{rng}]))', end))
    out["probe_coverage"] = min(1.0, (rtt_samples or 0) * scrape / (span * max(len(hubs), 1)))
    for h in hubs:
        s = f'lab="{lab}",customer="{customer}",hub="{h}"'
        out["hubs"][h] = {
            "rtt_avg": one(vm.q(f"avg_over_time(lab_sla_rtt_ms{{{s}}}[{rng}])", end)),
            "rtt_p95": one(vm.q(f"quantile_over_time(0.95, lab_sla_rtt_ms{{{s}}}[{rng}])", end)),
            "rtt_max": one(vm.q(f"max_over_time(lab_sla_rtt_ms{{{s}}}[{rng}])", end)),
            "loss": one(vm.q(f"avg_over_time(lab_sla_loss_ratio{{{s}}}[{rng}])", end)),
        }
    vals = [x for x in out["hubs"].values() if x["rtt_avg"] is not None]
    out["rtt_avg"] = sum(x["rtt_avg"] for x in vals) / len(vals) if vals else None
    out["rtt_p95"] = max((x["rtt_p95"] for x in vals if x["rtt_p95"] is not None), default=None)
    losses = [x["loss"] for x in out["hubs"].values() if x["loss"] is not None]
    out["loss"] = sum(losses) / len(losses) if losses else None
    pts = lambda res: [[int(t), float(v)] for t, v in res[0]["values"]] if res else []   # noqa: E731
    for h in hubs:
        s = f'lab="{lab}",customer="{customer}",hub="{h}"'
        out["series"][f"rtt:{h}"] = pts(vm.qr(f"avg_over_time(lab_sla_rtt_ms{{{s}}}[{step}s])", start, end, step))
        out["series"][f"loss:{h}"] = pts(vm.qr(f"avg_over_time(lab_sla_loss_ratio{{{s}}}[{step}s])", start, end, step))
    out["series"]["nhs_up"] = pts(vm.qr(f"min_over_time(lab_dmvpn_nhs_up{{{sel}}}[{step}s])", start, end, step))
    out["series"]["nhs_expected"] = pts(vm.qr(f"max_over_time(lab_dmvpn_nhs_expected{{{sel}}}[{step}s])", start, end, step))
    out["step"] = step

    def verdict(v, target, higher_better):
        if v is None:
            return None
        return v >= target if higher_better else v <= target
    out["met"] = {"availability": verdict(None if out["availability"] is None else out["availability"] * 100, TARGETS["availability"], True),
                  "rtt_ms": verdict(out["rtt_p95"], TARGETS["rtt_ms"], False),
                  "loss_pct": verdict(None if out["loss"] is None else out["loss"] * 100, TARGETS["loss_pct"], False)}
    return out
