"""Logins and roles for the portal.

Accounts live in auth/users.json on the lab host (not in git): a PBKDF2-SHA256 hash per password, never the password.
On first start the accounts in users.seed.json are created — the lab-default staff accounts, lab defaults like every
other credential of this lab (they are in the repository; change them in Lab Tools → Users). A signed cookie carries the
session (HMAC-SHA256 with a key made on first start, auth/secret.key); changing a password ends that user's sessions.

Roles (an account can hold several):
  viewer    reads everything (the map, the cloud, jobs, SLA, drift, resilience)
  operator  also starts jobs, files change requests, simulates failures, hands out customer links, backs up and restores
  approver  approves and rejects change requests (never one they filed: four eyes)
  admin     everything, including users and the change policy
  customer  one customer's own view and its self-service requests and diagnostics — nothing else

Open without a login: the page itself (it shows the sign-in form), /api/version, /api/auth/*, Prometheus' /metrics and
/api/sd, a customer's secret link (/c/<token>, /api/c/<token>/…), and GET /api/runs from the lab host itself (the lab hub
polls it)."""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = HERE / "auth"
DIR.mkdir(exist_ok=True)
USERS = DIR / "users.json"
SEED = HERE / "users.seed.json"
KEY = DIR / "secret.key"
COOKIE = "c8d_session"
SESSION_H = 12
ROLES = ("viewer", "operator", "approver", "admin", "customer")
RENDERER = "__renderer__"            # the portal's own headless Chrome (customer PDFs): a viewer that is no account
_lock = threading.Lock()


def _key():
    if not KEY.exists():
        KEY.write_bytes(secrets.token_bytes(32))
        KEY.chmod(0o600)
    return KEY.read_bytes()


def hash_pw(pw, salt=None, n=200_000):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2${n}${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), n).hex()}"


def check_pw(pw, stored):
    try:
        _, n, salt, h = stored.split("$")
        return hmac.compare_digest(hash_pw(pw, salt, int(n)).split("$")[3], h)
    except (ValueError, AttributeError):
        return False


def users():
    with _lock:
        if not USERS.exists():
            seed = json.loads(SEED.read_text()) if SEED.exists() else {"users": []}
            data = {u["username"]: {"name": u.get("name", u["username"]), "roles": u["roles"], "customer": u.get("customer"),
                                    "pw": hash_pw(u["password"]), "disabled": False, "created": time.time(), "seeded": True}
                    for u in seed["users"]}
            USERS.write_text(json.dumps(data, indent=1))
            USERS.chmod(0o600)
        return json.loads(USERS.read_text())


def _save(data):
    with _lock:
        USERS.write_text(json.dumps(data, indent=1))


def public(username, u):
    return {"username": username, "name": u.get("name"), "roles": u.get("roles", []), "customer": u.get("customer"),
            "disabled": bool(u.get("disabled")), "created": u.get("created")}


def authenticate(username, pw):
    u = users().get(username or "")
    if not u or u.get("disabled") or not check_pw(pw or "", u["pw"]):
        return None
    return u


def _sign(payload):
    return hmac.new(_key(), payload, hashlib.sha256).hexdigest()


def make_cookie(username, u=None):
    ver = (u or {}).get("pw", "")[-8:]                     # a new password ends the old sessions
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "exp": time.time() + SESSION_H * 3600, "v": ver}).encode())
    return payload.decode() + "." + _sign(payload)


def from_cookie(value):
    """The signed-in user {username, name, roles, customer}, or None."""
    try:
        payload, sig = (value or "").rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload.encode()), sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    if d.get("exp", 0) < time.time():
        return None
    if d.get("u") == RENDERER:
        return {"username": RENDERER, "name": "portal renderer", "roles": ["viewer"], "customer": None}
    u = users().get(d.get("u"))
    if not u or u.get("disabled") or u["pw"][-8:] != d.get("v"):
        return None
    return public(d["u"], u)


def renderer_cookie():
    payload = base64.urlsafe_b64encode(json.dumps({"u": RENDERER, "exp": time.time() + 600, "v": ""}).encode())
    return payload.decode() + "." + _sign(payload)


def has(user, *roles):
    """Does the user hold one of these roles? admin holds every staff role."""
    r = set((user or {}).get("roles") or [])
    return bool(r & set(roles)) or ("admin" in r and "customer" not in roles)


# ---- administration -----------------------------------------------------------------------------------------------
def create(username, name, roles, pw, customer=None):
    import re
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,30}", username or ""):
        raise ValueError("a username is 2-31 characters: lower case letters, digits, . _ -")
    bad = [r for r in roles if r not in ROLES]
    if bad or not roles:
        raise ValueError(f"roles are {', '.join(ROLES)}")
    if "customer" in roles and (len(roles) > 1 or not customer):
        raise ValueError("a customer account holds only the customer role, and names its customer")
    if len(pw or "") < 8:
        raise ValueError("a password has at least 8 characters")
    data = users()
    if username in data:
        raise ValueError(f"{username} exists")
    data[username] = {"name": name or username, "roles": roles, "customer": customer if "customer" in roles else None,
                      "pw": hash_pw(pw), "disabled": False, "created": time.time()}
    _save(data)
    return public(username, data[username])


def update(username, **f):
    data = users()
    if username not in data:
        raise KeyError(username)
    u = data[username]
    if "password" in f and f["password"] is not None:
        if len(f["password"]) < 8:
            raise ValueError("a password has at least 8 characters")
        u["pw"] = hash_pw(f["password"])
        u.pop("seeded", None)
    for k in ("name", "disabled"):
        if f.get(k) is not None:
            u[k] = f[k]
    if f.get("roles") is not None:
        if [r for r in f["roles"] if r not in ROLES] or not f["roles"]:
            raise ValueError(f"roles are {', '.join(ROLES)}")
        u["roles"] = f["roles"]
    if username == "admin" and ("admin" not in u["roles"] or u.get("disabled")):
        others = [n for n, x in data.items() if n != "admin" and "admin" in x["roles"] and not x.get("disabled")]
        if not others:
            raise ValueError("the last admin cannot lose the admin role or be disabled")
    _save(data)
    return public(username, u)


def delete(username):
    data = users()
    if username not in data:
        raise KeyError(username)
    if "admin" in data[username]["roles"] and not [n for n, x in data.items() if n != username and "admin" in x["roles"]]:
        raise ValueError("the last admin cannot be deleted")
    del data[username]
    _save(data)


def seeded_defaults():
    """Accounts still on their lab-default (seeded) password — shown to admins as a reminder."""
    return sorted(n for n, u in users().items() if u.get("seeded"))
