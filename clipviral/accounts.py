"""Users, sessions, workspaces (teams), invites, plans, usage metering and API keys.

Storage: SQLite (DATA_DIR/clipviral.db). Passwords use scrypt; session tokens
and API keys are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

DB_PATH = config.DATA_DIR / "clipviral.db"
SESSION_DAYS = 30
_lock = threading.RLock()

# Plans. Prices are display values; Stripe price IDs come from the environment.
PLANS: dict[str, dict] = {
    "free": {"label": "Free", "price_gbp": 0, "minutes": 60, "max_video_minutes": 30, "watermark": True,
             "brands": 1, "seats": 1, "api": False, "scheduler": False},
    "starter": {"label": "Starter", "price_gbp": 10, "minutes": 150, "max_video_minutes": 60, "watermark": False,
                "brands": 1, "seats": 1, "api": False, "scheduler": True},
    "creator": {"label": "Creator", "price_gbp": 15, "minutes": 400, "max_video_minutes": 120, "watermark": False,
                "brands": 3, "seats": 2, "api": False, "scheduler": True},
    "pro": {"label": "Pro", "price_gbp": 29, "minutes": 1000, "max_video_minutes": 180, "watermark": False,
            "brands": 10, "seats": 5, "api": True, "scheduler": True},
}
FREE_WATERMARK = "Made with ClipViral"


class AuthError(Exception):
    pass


class QuotaError(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, pw_hash TEXT NOT NULL, created_at REAL);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
  plan_status TEXT, stripe_customer_id TEXT, stripe_subscription_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS members (
  workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, created_at REAL,
  PRIMARY KEY (workspace_id, user_id));
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, email TEXT, created_by TEXT,
  created_at REAL, accepted_at REAL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  created_at REAL, expires_at REAL);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT, key_hash TEXT UNIQUE NOT NULL,
  prefix TEXT, created_at REAL, last_used_at REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL, job_id TEXT, minutes REAL NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS usage_ws ON usage (workspace_id, created_at);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, created_at REAL);
"""


@contextmanager
def db():
    with _lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

# simple in-memory brute-force protection: 8 failures per email per 15 minutes
_fails: dict[str, list[float]] = {}


def _throttled(email: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(email, []) if now - t < 900]
    _fails[email] = recent
    return len(recent) >= 8


# --------------------------------------------------------------------------- users & sessions
def signup(email: str, password: str, name: str = "", invite_token: str | None = None) -> tuple[dict, str]:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")
    name = (name or "").strip()[:80] or email.split("@")[0]
    if invite_token:
        info = invite_info(invite_token)
        if not info:
            raise AuthError("This invite link is invalid or was already used.")
        if info["email"] and info["email"] != email:
            raise AuthError(f"This invite is for {info['email']}. Sign up with that email to accept it.")
    with db() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise AuthError("An account with this email already exists. Log in instead.")
        first_user = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        uid = _id()
        con.execute("INSERT INTO users (id,email,name,pw_hash,created_at) VALUES (?,?,?,?,?)",
                    (uid, email, name, hash_password(password), time.time()))
        ws_id = _create_workspace(con, f"{name}'s workspace", uid)
    if first_user:
        _adopt_orphans(ws_id)
    current = ws_id
    if invite_token:
        current = accept_invite(invite_token, uid)
    return get_user(uid), new_session(uid, current)


def _create_workspace(con, name: str, owner_id: str) -> str:
    ws_id = _id()
    con.execute("INSERT INTO workspaces (id,name,owner_id,plan,created_at) VALUES (?,?,?,?,?)",
                (ws_id, name[:80], owner_id, "free", time.time()))
    con.execute("INSERT INTO members (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)",
                (ws_id, owner_id, "owner", time.time()))
    return ws_id


def _adopt_orphans(ws_id: str) -> None:
    """Projects/brands created before accounts existed belong to the first account."""
    import json as _json
    for p in config.JOBS_DIR.glob("*/job.json"):
        try:
            j = _json.loads(p.read_text())
            if not j.get("workspace_id"):
                j["workspace_id"] = ws_id
                p.write_text(_json.dumps(j, indent=1))
        except Exception:
            continue
    bdir = config.DATA_DIR / "brands"
    for p in bdir.glob("*/brand.json") if bdir.exists() else []:
        try:
            b = _json.loads(p.read_text())
            if not b.get("workspace_id"):
                b["workspace_id"] = ws_id
                p.write_text(_json.dumps(b, indent=1))
        except Exception:
            continue


def login(email: str, password: str) -> tuple[dict, str]:
    email = (email or "").strip().lower()
    if _throttled(email):
        raise AuthError("Too many attempts. Wait 15 minutes and try again.")
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password(password or "", row["pw_hash"]):
        _fails.setdefault(email, []).append(time.time())
        raise AuthError("Wrong email or password.")
    _fails.pop(email, None)
    ws = workspaces_for(row["id"])
    return get_user(row["id"]), new_session(row["id"], ws[0]["id"])


def new_session(user_id: str, workspace_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO sessions (token_hash,user_id,workspace_id,created_at,expires_at) VALUES (?,?,?,?,?)",
                    (_h(token), user_id, workspace_id, now, now + SESSION_DAYS * 86400))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def logout(token: str) -> None:
    with db() as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


def session_context(token: str | None) -> dict | None:
    if not token:
        return None
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at > ?", (_h(token), time.time())).fetchone()
        if not s:
            return None
        m = con.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?", (s["workspace_id"], s["user_id"])).fetchone()
        if not m:  # removed from that workspace: fall back to one they still belong to
            other = con.execute("SELECT workspace_id, role FROM members WHERE user_id=? LIMIT 1", (s["user_id"],)).fetchone()
            if not other:
                return None
            con.execute("UPDATE sessions SET workspace_id=? WHERE token_hash=?", (other["workspace_id"], _h(token)))
            ws_id, role = other["workspace_id"], other["role"]
        else:
            ws_id, role = s["workspace_id"], m["role"]
    return {"user": get_user(s["user_id"]), "workspace": get_workspace(ws_id), "role": role, "via": "session"}


def switch_workspace(token: str, user_id: str, workspace_id: str) -> None:
    with db() as con:
        if not con.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)).fetchone():
            raise AuthError("You're not a member of that workspace.")
        con.execute("UPDATE sessions SET workspace_id=? WHERE token_hash=?", (workspace_id, _h(token)))


def get_user(uid: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT id,email,name,created_at FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def get_workspace(ws_id: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    return dict(r) if r else None


def workspaces_for(user_id: str) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT w.*, m.role FROM workspaces w JOIN members m ON m.workspace_id=w.id "
                           "WHERE m.user_id=? ORDER BY m.created_at", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def rename_workspace(ws_id: str, name: str) -> None:
    name = (name or "").strip()[:80]
    if not name:
        raise AuthError("Workspace name can't be empty.")
    with db() as con:
        con.execute("UPDATE workspaces SET name=? WHERE id=?", (name, ws_id))


# --------------------------------------------------------------------------- teams
def members(ws_id: str) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT u.id, u.email, u.name, m.role FROM members m JOIN users u ON u.id=m.user_id "
                           "WHERE m.workspace_id=? ORDER BY m.created_at", (ws_id,)).fetchall()
    return [dict(r) for r in rows]


def pending_invites(ws_id: str) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT email, created_at, substr(token_hash,1,12) AS id FROM invites "
                           "WHERE workspace_id=? AND accepted_at IS NULL ORDER BY created_at", (ws_id,)).fetchall()
    return [dict(r) for r in rows]


def create_invite(ws_id: str, by_user: str, email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    plan = plan_of(ws_id)
    if len(members(ws_id)) + len(pending_invites(ws_id)) >= plan["seats"]:
        raise QuotaError(f"Your {plan['label']} plan includes {plan['seats']} seat(s). Upgrade to add teammates.")
    token = secrets.token_urlsafe(24)
    with db() as con:
        con.execute("INSERT INTO invites (token_hash,workspace_id,email,created_by,created_at) VALUES (?,?,?,?,?)",
                    (_h(token), ws_id, email, by_user, time.time()))
    return token


def revoke_invite(ws_id: str, invite_id: str) -> None:
    with db() as con:
        con.execute("DELETE FROM invites WHERE workspace_id=? AND substr(token_hash,1,12)=? AND accepted_at IS NULL",
                    (ws_id, invite_id))


def invite_info(token: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT i.email, w.name AS workspace FROM invites i JOIN workspaces w ON w.id=i.workspace_id "
                        "WHERE i.token_hash=? AND i.accepted_at IS NULL", (_h(token),)).fetchone()
    return dict(r) if r else None


def accept_invite(token: str, user_id: str) -> str:
    with db() as con:
        inv = con.execute("SELECT * FROM invites WHERE token_hash=? AND accepted_at IS NULL", (_h(token),)).fetchone()
        if not inv:
            raise AuthError("This invite link is invalid or was already used.")
        if time.time() - inv["created_at"] > 14 * 86400:
            raise AuthError("This invite link has expired. Ask for a new one.")
        user = con.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        if inv["email"] and user and user["email"] != inv["email"]:
            raise AuthError(f"This invite is for {inv['email']}. Log in with that email to accept it.")
        con.execute("INSERT OR IGNORE INTO members (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)",
                    (inv["workspace_id"], user_id, "member", time.time()))
        con.execute("UPDATE invites SET accepted_at=? WHERE token_hash=?", (time.time(), _h(token)))
        return inv["workspace_id"]


def remove_member(ws_id: str, user_id: str) -> None:
    ws = get_workspace(ws_id)
    if ws and ws["owner_id"] == user_id:
        raise AuthError("The workspace owner can't be removed.")
    with db() as con:
        con.execute("DELETE FROM members WHERE workspace_id=? AND user_id=?", (ws_id, user_id))


# --------------------------------------------------------------------------- plans & usage
def plan_of(ws_id: str) -> dict:
    ws = get_workspace(ws_id)
    key = ws["plan"] if ws and ws["plan"] in PLANS else "free"
    return {"key": key, **PLANS[key]}


def set_plan(ws_id: str, plan: str, status: str | None = None, customer: str | None = None,
             subscription: str | None = None) -> None:
    if plan not in PLANS:
        raise ValueError(plan)
    with db() as con:
        con.execute("UPDATE workspaces SET plan=?, plan_status=?, "
                    "stripe_customer_id=COALESCE(?, stripe_customer_id), "
                    "stripe_subscription_id=COALESCE(?, stripe_subscription_id) WHERE id=?",
                    (plan, status, customer, subscription, ws_id))


def _month_start(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


def minutes_used(ws_id: str) -> float:
    with db() as con:
        v = con.execute("SELECT COALESCE(SUM(minutes),0) FROM usage WHERE workspace_id=? AND created_at>=?",
                        (ws_id, _month_start())).fetchone()[0]
    return round(float(v), 1)


def usage_summary(ws_id: str) -> dict:
    plan = plan_of(ws_id)
    used = minutes_used(ws_id)
    nxt = datetime.fromtimestamp(_month_start(), tz=timezone.utc)
    reset = datetime(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1, tzinfo=timezone.utc)
    return {"plan": plan, "minutes_used": used, "minutes_limit": plan["minutes"],
            "minutes_left": max(0.0, round(plan["minutes"] - used, 1)), "resets_at": reset.timestamp()}


def charge(ws_id: str, job_id: str, video_minutes: float) -> None:
    """Reserve minutes for a job, or raise QuotaError with a friendly message."""
    plan = plan_of(ws_id)
    if video_minutes > plan["max_video_minutes"]:
        raise QuotaError(f"This video is {video_minutes:.0f} minutes long. The {plan['label']} plan accepts videos "
                         f"up to {plan['max_video_minutes']} minutes. Upgrade for longer videos.")
    with db() as con:
        used = con.execute("SELECT COALESCE(SUM(minutes),0) FROM usage WHERE workspace_id=? AND created_at>=?",
                           (ws_id, _month_start())).fetchone()[0]
        if used + video_minutes > plan["minutes"] + 1e-6:
            left = max(0.0, plan["minutes"] - used)
            raise QuotaError(f"This video needs {video_minutes:.0f} minutes but you have {left:.0f} of your "
                             f"{plan['minutes']} monthly minutes left. Upgrade or wait until next month.")
        if con.execute("SELECT 1 FROM usage WHERE workspace_id=? AND job_id=?", (ws_id, job_id)).fetchone():
            return  # already charged (e.g. job resumed after a restart)
        con.execute("INSERT INTO usage (workspace_id,job_id,minutes,created_at) VALUES (?,?,?,?)",
                    (ws_id, job_id, round(video_minutes, 2), time.time()))


def refund(ws_id: str, job_id: str) -> None:
    with db() as con:
        con.execute("DELETE FROM usage WHERE workspace_id=? AND job_id=?", (ws_id, job_id))


# --------------------------------------------------------------------------- API keys
def create_api_key(ws_id: str, name: str) -> tuple[dict, str]:
    if not plan_of(ws_id)["api"]:
        raise QuotaError("API access is included in the Pro plan.")
    key = "cv_live_" + secrets.token_urlsafe(32)
    kid = _id()
    with db() as con:
        con.execute("INSERT INTO api_keys (id,workspace_id,name,key_hash,prefix,created_at) VALUES (?,?,?,?,?,?)",
                    (kid, ws_id, (name or "API key").strip()[:60], _h(key), key[:12], time.time()))
    return {"id": kid, "name": name, "prefix": key[:12]}, key


def list_api_keys(ws_id: str) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT id,name,prefix,created_at,last_used_at FROM api_keys WHERE workspace_id=? AND revoked=0 "
                           "ORDER BY created_at", (ws_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(ws_id: str, kid: str) -> None:
    with db() as con:
        con.execute("UPDATE api_keys SET revoked=1 WHERE workspace_id=? AND id=?", (ws_id, kid))


def api_key_context(key: str | None) -> dict | None:
    if not key or not key.startswith("cv_live_"):
        return None
    with db() as con:
        r = con.execute("SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (_h(key),)).fetchone()
        if not r:
            return None
        con.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), r["id"]))
    ws = get_workspace(r["workspace_id"])
    if not ws or not plan_of(ws["id"])["api"]:
        return None  # downgraded below Pro: keys stop working
    return {"user": None, "workspace": ws, "role": "api", "via": "api_key"}
