"""Change control: disruptive work needs a second person's approval, and runs inside a change window.

A request the policy covers — removing, modifying or restoring customers, deploying, fixing drift, simulating a failure,
measuring a failover — does not start: it becomes a change request (CR-0001 …). Someone other than the requester
approves it (four eyes) or rejects it. An approved change starts at once if a change window is open (or windows are off,
or the approver declares an emergency with a reason); otherwise it is scheduled and the portal starts it when the next
window opens. Adding a customer, dry runs, tests and drift checks never need approval: they take nothing away.

Requesters and approvers are the signed-in accounts (auth.py): an operator files, an approver decides, never their own.
A customer's own requests (its portal) are change requests too, always waiting for an approver.

   policy.json   the policy (defaults below; edited in the portal)
   requests/     one JSON per change request"""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent / "changes"
REQ = DIR / "requests"
REQ.mkdir(parents=True, exist_ok=True)
POLICY = DIR / "policy.json"
_lock = threading.Lock()

# what can need approval: run modes, plus "fault" (simulated failures)
COVERABLE = {"remove": "Remove a customer", "modify": "Modify a customer", "restore": "Restore from a backup",
             "deploy": "Deploy the model", "fixdrift": "Fix drift", "failover": "Measure a failover", "fault": "Simulate a failure",
             "rotatepsk": "Rotate the pre-shared key"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT = {
    "approval": {"enabled": True, "covers": ["remove", "modify", "restore", "deploy", "fixdrift", "failover", "fault", "rotatepsk"],
                 "four_eyes": True},
    "windows": {"enabled": False, "slots": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "20:00", "end": "06:00"},
                                            {"days": ["sat", "sun"], "start": "00:00", "end": "23:59"}]},
    "emergency": True,
    "expire_hours": 72,
}


def policy():
    try:
        p = json.loads(POLICY.read_text())
        return {**DEFAULT, **p, "approval": {**DEFAULT["approval"], **p.get("approval", {})},
                "windows": {**DEFAULT["windows"], **p.get("windows", {})}}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT))


def validate_policy(p):
    probs = []
    for c in p.get("approval", {}).get("covers", []):
        if c not in COVERABLE:
            probs.append(f"{c} is not something approval can cover ({', '.join(COVERABLE)})")
    for i, sl in enumerate(p.get("windows", {}).get("slots", [])):
        if not sl.get("days") or any(d not in DAYS for d in sl["days"]):
            probs.append(f"window {i + 1}: days are {', '.join(DAYS)}")
        for k in ("start", "end"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(sl.get(k, ""))):
                probs.append(f"window {i + 1}: {k} is HH:MM")
    if p.get("windows", {}).get("enabled") and not p.get("windows", {}).get("slots"):
        probs.append("windows are on but none is defined: nothing could ever run")
    return probs


def save_policy(p):
    POLICY.write_text(json.dumps(p, indent=1))


def covered(kind):
    p = policy()
    return p["approval"]["enabled"] and kind in p["approval"]["covers"]


def _in_slot(sl, dt):
    """A slot may run past midnight (20:00-06:00): the early hours belong to the day it started."""
    st, en = [datetime.strptime(sl[k], "%H:%M").time() for k in ("start", "end")]
    day, t = DAYS[dt.weekday()], dt.time()
    prev = DAYS[(dt.weekday() - 1) % 7]
    if st <= en:
        return day in sl["days"] and st <= t <= en
    return (day in sl["days"] and t >= st) or (prev in sl["days"] and t <= en)


def window_open(at=None):
    p = policy()
    if not p["windows"]["enabled"]:
        return True
    dt = datetime.fromtimestamp(at or time.time())
    return any(_in_slot(sl, dt) for sl in p["windows"]["slots"])


def next_window(at=None):
    """When the next window opens (epoch seconds), searching a week ahead by the minute; None if windows are off."""
    p = policy()
    if not p["windows"]["enabled"]:
        return None
    dt = datetime.fromtimestamp(at or time.time()).replace(second=0, microsecond=0)
    for i in range(7 * 24 * 60):
        t = dt + timedelta(minutes=i)
        if any(_in_slot(sl, t) for sl in p["windows"]["slots"]):
            return t.timestamp()
    return None


def _next_id():
    nums = [int(m[1]) for p in REQ.glob("CR-*.json") if (m := re.match(r"CR-(\d+)", p.stem))]
    return f"CR-{max(nums, default=0) + 1:04d}"


def _save(cr):
    (REQ / f"{cr['id']}.json").write_text(json.dumps(cr, indent=1))


def get(cid):
    p = REQ / f"{cid}.json"
    if "/" in cid or not p.exists():
        return None
    return json.loads(p.read_text())


def all_requests(limit=50):
    out = []
    for p in sorted(REQ.glob("CR-*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            pass
    return out


def create(kind, request, summary, by, reason="", affects=None, source="staff"):
    """A change request for work the policy covers. `request` is what starts it later (a RunRequest or a fault)."""
    if not str(by or "").strip():
        raise ValueError("say who is asking (requested_by)")
    with _lock:
        cr = {"id": _next_id(), "kind": kind, "title": COVERABLE.get(kind, kind), "summary": summary, "request": request,
              "requested_by": by.strip(), "requested_at": time.time(), "reason": reason, "status": "pending",
              "affects": affects or [], "source": source,
              "events": [{"t": time.time(), "by": by.strip(), "what": "requested", "note": reason}]}
        _save(cr)
        return cr


def decide(cid, by, approve, comment="", emergency=False):
    """Approve or reject. Returns the request; its status is then approved (start now), scheduled or rejected."""
    by = str(by or "").strip()
    if not by:
        raise ValueError("say who is deciding")
    with _lock:
        cr = get(cid)
        if cr is None:
            raise KeyError(cid)
        if cr["status"] != "pending":
            raise ValueError(f"{cid} is {cr['status']}")
        p = policy()
        if p["approval"]["four_eyes"] and by.lower() == cr["requested_by"].lower():
            raise PermissionError(f"{by} asked for {cid}: someone else has to approve or reject it (four eyes)")
        if not approve:
            cr["status"] = "rejected"
        elif emergency:
            if not p["emergency"]:
                raise PermissionError("the policy allows no emergency changes")
            if not comment.strip():
                raise ValueError("an emergency change needs a reason")
            cr["status"], cr["emergency"] = "approved", True
        elif window_open():
            cr["status"] = "approved"
        else:
            cr["status"], cr["scheduled_for"] = "scheduled", next_window()
        cr.update({"decided_by": by, "decided_at": time.time(), "comment": comment})
        cr["events"].append({"t": time.time(), "by": by, "what": ("approved" + (" (emergency)" if emergency else "")) if approve
                             else "rejected", "note": comment})
        _save(cr)
        return cr


def cancel(cid, by):
    with _lock:
        cr = get(cid)
        if cr is None:
            raise KeyError(cid)
        if cr["status"] not in ("pending", "scheduled"):
            raise ValueError(f"{cid} is {cr['status']}")
        cr["status"] = "cancelled"
        cr["events"].append({"t": time.time(), "by": by or "?", "what": "cancelled", "note": ""})
        _save(cr)
        return cr


def mark(cid, status, what, **fields):
    with _lock:
        cr = get(cid)
        cr.update({"status": status, **fields})
        cr["events"].append({"t": time.time(), "by": "portal", "what": what, "note": fields.get("error", "")})
        _save(cr)
        return cr


def due():
    """Scheduled requests whose window is open now, and pending ones too old to keep waiting."""
    out, old = [], []
    exp = policy().get("expire_hours") or 0
    for cr in all_requests(500):
        if cr["status"] == "scheduled" and window_open():
            out.append(cr)
        elif cr["status"] == "pending" and exp and time.time() - cr["requested_at"] > exp * 3600:
            old.append(cr)
    return out, old
