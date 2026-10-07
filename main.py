"""
TG INFO — Telegram Username / Chat-ID Lookup API
Developer : @Exodus_OWN3R  |  Version 1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET /lookup?key=YOUR_KEY&tg=@username
GET /lookup?key=YOUR_KEY&tg=12345678901
GET /key=YOUR_KEY&tg=@username          (path-style)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run:  python main.py
"""

import sys, os

def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_dotenv()

from flask import (Flask, request, jsonify, session as flask_session,
                   redirect, render_template_string, send_file, url_for)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.channels import GetFullChannelRequest
import asyncio, threading, time, logging, secrets, json, re, io
from datetime import datetime, timezone
from functools import wraps
import urllib.request, urllib.parse

import botsys

# ══════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "Exodus_OWN3R")
DEVELOPER       = "@Exodus_OWN3R"
PORT            = int(os.environ.get("PORT", 5000))
SECRET_KEY      = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ACCOUNTS_FILE   = "accounts_data.json"
EXT_APIS_FILE   = "ext_apis.json"
START_TIME      = time.time()

logging.basicConfig(level=logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# global counters
_total_lookups  = 0
_total_success  = 0
_counter_lock   = threading.Lock()

# ══════════════════════════════════════════════════════
#  EXTERNAL API MANAGEMENT
# ══════════════════════════════════════════════════════
_ext_apis = {}      # id → {id, name, base_url, key_param, key_value, tg_param, enabled, priority, added_at}
_ext_lock = threading.Lock()


def _load_ext_apis():
    global _ext_apis
    if not os.path.exists(EXT_APIS_FILE):
        return
    try:
        with open(EXT_APIS_FILE) as f:
            _ext_apis = json.load(f)
    except Exception as e:
        print(f"[ExtAPI] load error: {e}")


def _save_ext_apis():
    try:
        with _ext_lock:
            with open(EXT_APIS_FILE, "w") as f:
                json.dump(_ext_apis, f, indent=2)
    except Exception as e:
        print(f"[ExtAPI] save error: {e}")


def _call_ext_api(api_conf, target):
    try:
        params = {
            api_conf.get("key_param", "key"): api_conf.get("key_value", ""),
            api_conf.get("tg_param",  "tg"):  target,
        }
        url = api_conf["base_url"].rstrip("/") + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "TGInfo/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        if data.get("status") is True or data.get("ok") is True:
            data["_source"] = api_conf["name"]
            return data
    except Exception as e:
        print(f"[ExtAPI] {api_conf.get('name')} error: {e}")
    return None


def _try_ext_apis(target):
    with _ext_lock:
        apis = sorted([a for a in _ext_apis.values() if a.get("enabled")],
                      key=lambda x: x.get("priority", 99))
    for api in apis:
        r = _call_ext_api(api, target)
        if r:
            return r
    return None


# ══════════════════════════════════════════════════════
#  TELETHON ACCOUNTS
# ══════════════════════════════════════════════════════
accounts      = {}
accounts_lock = threading.RLock()
_rr_idx       = 0
_rr_lock      = threading.Lock()


def _save_accounts():
    try:
        snap = {}
        with accounts_lock:
            for aid, a in accounts.items():
                snap[aid] = {k: v for k, v in a.items()
                             if k not in ("client", "loop", "thread", "ready", "error")}
        with open(ACCOUNTS_FILE, "w") as f:
            json.dump(snap, f, indent=2)
    except Exception as e:
        print(f"[Accounts] save error: {e}")


def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _start_account(aid):
    with accounts_lock:
        acc = accounts.get(aid)
        if not acc:
            return
        if acc.get("thread") and acc["thread"].is_alive():
            return
        loop = asyncio.new_event_loop()
        acc.update(loop=loop, ready=False, error=None)
        t = threading.Thread(target=_run_loop, args=(loop,), daemon=True, name=f"tg-{aid}")
        t.start()
        acc["thread"] = t

    async def _connect():
        try:
            cl = TelegramClient(StringSession(acc["session"]),
                                int(acc["api_id"]), acc["api_hash"])
            await cl.connect()
            if not await cl.is_user_authorized():
                raise Exception("Session expired / not authorized")
            me = await cl.get_me()
            with accounts_lock:
                acc["client"] = cl
                acc["ready"]  = True
                acc["error"]  = None
                acc["name"]   = f"{me.first_name or ''} {me.last_name or ''}".strip() or aid
            print(f"[Accounts] {aid} → {acc['name']}", flush=True)
        except Exception as e:
            with accounts_lock:
                acc["ready"] = False
                acc["error"] = str(e)
            print(f"[Accounts] {aid} error: {e}", flush=True)

    asyncio.run_coroutine_threadsafe(_connect(), acc["loop"])


def _stop_account(aid):
    with accounts_lock:
        acc = accounts.get(aid)
        if not acc:
            return
        cl, loop = acc.get("client"), acc.get("loop")

    async def _dc():
        try: await cl.disconnect()
        except: pass

    if cl and loop:
        try: asyncio.run_coroutine_threadsafe(_dc(), loop).result(timeout=5)
        except: pass
    if loop:
        loop.call_soon_threadsafe(loop.stop)
    with accounts_lock:
        if aid in accounts:
            accounts[aid].update(client=None, ready=False)


def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return
    try:
        with open(ACCOUNTS_FILE) as f:
            data = json.load(f)
        for aid, a in data.items():
            accounts[aid] = {
                **a, "id": aid,
                "client": None, "loop": None, "thread": None,
                "ready":  False, "error": None,
                "lookup_count":  a.get("lookup_count",  0),
                "success_count": a.get("success_count", 0),
                "fail_count":    a.get("fail_count",    0),
                "last_used":     a.get("last_used",     ""),
                "added_at":      a.get("added_at",      ""),
                "paused":        a.get("paused",        False),
            }
        print(f"[Accounts] Loaded {len(accounts)}")
        for aid, acc in accounts.items():
            if not acc.get("paused"):
                _start_account(aid)
    except Exception as e:
        print(f"[Accounts] load error: {e}")


def _next_account():
    global _rr_idx
    with accounts_lock:
        ready = [k for k, v in accounts.items() if v.get("ready") and not v.get("paused")]
    if not ready:
        return None, None
    with _rr_lock:
        idx = _rr_idx % len(ready)
        _rr_idx += 1
    aid = ready[idx]
    with accounts_lock:
        return aid, accounts.get(aid)


# ══════════════════════════════════════════════════════
#  CORE LOOKUP LOGIC
# ══════════════════════════════════════════════════════

async def _telethon_resolve(client, raw):
    from telethon.tl.types import (
        User, Channel, Chat,
        UserStatusOnline, UserStatusOffline, UserStatusRecently,
        UserStatusLastWeek, UserStatusLastMonth,
    )
    t = str(raw).strip()
    if t.startswith("@"):
        parsed = t[1:]
    elif t.lstrip("-").isdigit():
        parsed = int(t)
    else:
        parsed = t

    entity = await client.get_entity(parsed)
    out = {"status": True, "developer": DEVELOPER, "powered_by": "@mainexodus", "query": raw}

    if isinstance(entity, User):
        st = entity.status
        if   isinstance(st, UserStatusOnline):    us = "online"
        elif isinstance(st, UserStatusOffline):   us = "offline"
        elif isinstance(st, UserStatusRecently):  us = "recently"
        elif isinstance(st, UserStatusLastWeek):  us = "last_week"
        elif isinstance(st, UserStatusLastMonth): us = "last_month"
        else:                                     us = "unknown"

        out.update({
            "type":          "bot" if entity.bot else "user",
            "id":            entity.id,
            "username":      f"@{entity.username}" if entity.username else None,
            "first_name":    entity.first_name,
            "last_name":     entity.last_name,
            "phone":         entity.phone,
            "is_bot":        bool(entity.bot),
            "is_verified":   bool(entity.verified),
            "is_premium":    bool(entity.premium),
            "is_scam":       bool(entity.scam),
            "is_fake":       bool(entity.fake),
            "is_restricted": bool(entity.restricted),
            "is_deleted":    bool(entity.deleted),
            "has_photo":     entity.photo is not None,
            "dc_id":         getattr(entity.photo, "dc_id", None),
            "user_status":   us,
            "profile_url":   (f"https://t.me/{entity.username}" if entity.username
                              else f"tg://user?id={entity.id}"),
        })
        try:
            full = await client(GetFullUserRequest(entity))
            fu   = full.full_user
            out["bio"]          = fu.about or None
            out["common_chats"] = fu.common_chats_count
        except:
            out["bio"] = None; out["common_chats"] = None

    elif isinstance(entity, Channel):
        out.update({
            "type":          "supergroup" if entity.megagroup else "channel",
            "id":            int(f"-100{entity.id}"),
            "title":         entity.title,
            "username":      f"@{entity.username}" if entity.username else None,
            "is_verified":   bool(entity.verified),
            "is_scam":       bool(entity.scam),
            "is_fake":       bool(entity.fake),
            "is_restricted": bool(entity.restricted),
            "is_megagroup":  bool(entity.megagroup),
            "is_broadcast":  bool(entity.broadcast),
            "has_photo":     entity.photo is not None,
            "dc_id":         getattr(entity.photo, "dc_id", None),
            "members_count": getattr(entity, "participants_count", None),
            "profile_url":   f"https://t.me/{entity.username}" if entity.username else None,
        })
        try:
            full = await client(GetFullChannelRequest(entity))
            fc   = full.full_chat
            out["description"]  = fc.about or None
            out["admins_count"] = fc.admins_count
            out["banned_count"] = fc.banned_count
            if out["members_count"] is None:
                out["members_count"] = fc.participants_count
        except:
            out["description"] = None

    elif isinstance(entity, Chat):
        out.update({
            "type": "group", "id": int(f"-{entity.id}"), "title": entity.title,
            "username": None, "members_count": getattr(entity, "participants_count", None),
            "has_photo": entity.photo is not None, "dc_id": None,
            "profile_url": None, "description": None,
        })

    return out


def do_lookup(target: str):
    global _total_lookups, _total_success
    with _counter_lock:
        _total_lookups += 1

    aid, acc = _next_account()
    if acc:
        try:
            fut    = asyncio.run_coroutine_threadsafe(
                         _telethon_resolve(acc["client"], target), acc["loop"])
            result = fut.result(timeout=20)
            with accounts_lock:
                if aid in accounts:
                    accounts[aid]["lookup_count"]  += 1
                    accounts[aid]["success_count"] += 1
                    accounts[aid]["last_used"]      = datetime.now(timezone.utc).isoformat()
            _save_accounts()
            with _counter_lock:
                _total_success += 1
            return result, 200
        except Exception as e:
            with accounts_lock:
                if aid in accounts:
                    accounts[aid]["fail_count"] += 1
            tel_err = str(e)
    else:
        tel_err = "No Telethon accounts configured"

    ext = _try_ext_apis(target)
    if ext:
        ext["fallback_source"] = ext.pop("_source", "external")
        with _counter_lock:
            _total_success += 1
        return ext, 200

    return {"status": False, "developer": DEVELOPER, "error": tel_err,
            "hint": "Add a Telethon account or external API via /admin"}, 503


# ══════════════════════════════════════════════════════
#  FLASK AUTH
# ══════════════════════════════════════════════════════
def _is_admin():
    return flask_session.get("admin") is True


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        if not _is_admin():
            return redirect("/admin/login")
        return fn(*a, **kw)
    return w


# ══════════════════════════════════════════════════════
#  PUBLIC ROUTES
# ══════════════════════════════════════════════════════

@app.route("/lookup")
def lookup_route():
    tg = (request.args.get("tg") or "").strip()
    if not tg:
        return jsonify({"status": False, "developer": DEVELOPER,
                        "error": "Missing ?tg= parameter",
                        "example": "?key=YOUR_KEY&tg=@username"}), 400
    data, code = do_lookup(tg)
    return jsonify(data), code


@app.route("/<path:raw>")
def lookup_path_style(raw):
    """Handles /key=xxx&tg=@username path-style URLs."""
    if raw.startswith("admin"):
        return redirect(f"/{raw}")
    try:
        pairs  = dict(p.split("=", 1) for p in raw.split("&") if "=" in p)
        tg     = pairs.get("tg", "").strip()
        if not tg:
            return jsonify({"status": False, "error": "Missing tg param"}), 400
        key    = pairs.get("key", "")
        if key:
            ok, code, msg = botsys.validate_and_count(key)
            if not ok:
                return jsonify({"status": False, "developer": DEVELOPER, "message": msg}), code
        data, hcode = do_lookup(tg)
        return jsonify(data), hcode
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 400


@app.route("/")
def index():
    bot_username = botsys.BOT_USERNAME or "your_bot"
    tlu = _total_lookups
    tls = _total_success
    nacc = sum(1 for a in accounts.values() if a.get("ready"))
    uptime_s = int(time.time() - START_TIME)
    up_h, up_m = uptime_s // 3600, (uptime_s % 3600) // 60
    return render_template_string(LANDING_HTML,
        bot_username=bot_username, total_lookups=tlu, total_success=tls,
        ready_accounts=nacc, uptime=f"{up_h}h {up_m}m",
        ext_count=len([a for a in _ext_apis.values() if a.get("enabled")]))


@app.route("/docs")
def docs_page():
    bot_username = botsys.BOT_USERNAME or "your_bot"
    base = os.environ.get("BASE_URL", f"http://localhost:{PORT}").rstrip("/")
    return render_template_string(DOCS_HTML, base=base, bot_username=bot_username)


@app.route("/check", methods=["GET", "POST"])
def check_key_page():
    result = None
    key    = ""
    if request.method == "POST":
        key = (request.form.get("key") or "").strip()
        if key:
            import sqlite3
            try:
                row = botsys.q("SELECT k.*, u.username, u.first_name, u.total_refs "
                               "FROM keys k LEFT JOIN users u ON u.id=k.user_id WHERE k.key=?",
                               (key,), one=True)
                if row:
                    usage = botsys.key_usage_today(key)
                    lim   = row["daily_limit"] or botsys.daily_limit()
                    result = {
                        "found": True,
                        "key":    key,
                        "user":   f"@{row['username']}" if row["username"] else row["first_name"] or "—",
                        "plan":   row["plan"] or "Standard",
                        "expires": datetime.fromtimestamp(row["expires_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if row["expires_at"] else "—",
                        "expired": row["expires_at"] < int(time.time()),
                        "revoked": bool(row["revoked"]),
                        "usage":  usage,
                        "limit":  lim,
                        "pct":    min(100, int(usage / lim * 100)) if lim else 0,
                    }
                else:
                    result = {"found": False}
            except Exception as e:
                result = {"found": False, "error": str(e)}
    return render_template_string(CHECK_HTML, result=result, key=key)


# ══════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            flask_session["admin"] = True
            return redirect("/admin/dashboard")
        err = "Wrong password"
    return render_template_string(LOGIN_HTML, err=err)


@app.route("/admin")
def admin_root():
    if _is_admin():
        return redirect("/admin/dashboard")
    return redirect("/admin/login")


@app.route("/admin/logout")
def admin_logout():
    flask_session.clear()
    return redirect("/admin/login")


@app.route("/admin/dashboard")
@_require_admin
def admin_dashboard():
    with accounts_lock:
        accs = list(accounts.values())
    with _ext_lock:
        ext  = list(_ext_apis.values())
    users_count   = botsys.q("SELECT COUNT(*) AS n FROM users", one=True)["n"]
    keys_active   = botsys.q("SELECT COUNT(*) AS n FROM keys WHERE revoked=0 AND expires_at>?",
                              (int(time.time()),), one=True)["n"]
    chans         = botsys.q("SELECT COUNT(*) AS n FROM channels", one=True)["n"]
    uptime_s      = int(time.time() - START_TIME)
    up_h, up_m    = uptime_s // 3600, (uptime_s % 3600) // 60
    return render_template_string(ADMIN_HTML, section="dashboard",
        accs=accs, ext=ext,
        total_lookups=_total_lookups, total_success=_total_success,
        uptime=f"{up_h}h {up_m}m", users_count=users_count,
        keys_active=keys_active, chans=chans)


@app.route("/admin/accounts", methods=["GET"])
@_require_admin
def admin_accounts():
    with accounts_lock:
        accs = list(accounts.values())
    return render_template_string(ADMIN_HTML, section="accounts", accs=accs)


@app.route("/admin/accounts/add", methods=["POST"])
@_require_admin
def admin_accounts_add():
    session_str = (request.form.get("session") or "").strip()
    api_id      = (request.form.get("api_id") or "").strip()
    api_hash    = (request.form.get("api_hash") or "").strip()
    name        = (request.form.get("name") or "").strip() or f"Account {len(accounts)+1}"
    if not (session_str and api_id and api_hash):
        return redirect("/admin/accounts")
    aid = f"acc_{int(time.time())}"
    with accounts_lock:
        accounts[aid] = {
            "id": aid, "name": name,
            "session": session_str, "api_id": int(api_id), "api_hash": api_hash,
            "paused": False, "lookup_count": 0, "success_count": 0, "fail_count": 0,
            "last_used": "", "added_at": datetime.now(timezone.utc).isoformat(),
            "client": None, "loop": None, "thread": None, "ready": False, "error": None,
        }
    _save_accounts()
    _start_account(aid)
    return redirect("/admin/accounts")


@app.route("/admin/accounts/<aid>/remove", methods=["POST"])
@_require_admin
def admin_accounts_remove(aid):
    _stop_account(aid)
    with accounts_lock:
        accounts.pop(aid, None)
    _save_accounts()
    return redirect("/admin/accounts")


@app.route("/admin/accounts/<aid>/toggle", methods=["POST"])
@_require_admin
def admin_accounts_toggle(aid):
    with accounts_lock:
        acc = accounts.get(aid)
        if not acc:
            return redirect("/admin/accounts")
        acc["paused"] = not acc["paused"]
    if not accounts[aid]["paused"]:
        _start_account(aid)
    else:
        _stop_account(aid)
    _save_accounts()
    return redirect("/admin/accounts")


@app.route("/admin/accounts/<aid>/update-session", methods=["POST"])
@_require_admin
def admin_update_session(aid):
    new_session = (request.form.get("session") or "").strip()
    if not new_session:
        return redirect("/admin/accounts")
    _stop_account(aid)
    with accounts_lock:
        if aid in accounts:
            accounts[aid]["session"] = new_session
    _save_accounts()
    _start_account(aid)
    return redirect("/admin/accounts")


# ── External APIs ────────────────────────────────────

@app.route("/admin/ext-apis", methods=["GET"])
@_require_admin
def admin_ext_apis():
    with _ext_lock:
        ext = list(_ext_apis.values())
    return render_template_string(ADMIN_HTML, section="ext_apis", ext=ext)


@app.route("/admin/ext-apis/add", methods=["POST"])
@_require_admin
def admin_ext_apis_add():
    f = request.form
    eid  = f"ext_{int(time.time())}"
    name = (f.get("name") or "External API").strip()
    obj  = {
        "id":        eid,
        "name":      name,
        "base_url":  (f.get("base_url") or "").strip(),
        "key_param": (f.get("key_param") or "key").strip(),
        "key_value": (f.get("key_value") or "").strip(),
        "tg_param":  (f.get("tg_param") or "tg").strip(),
        "priority":  int(f.get("priority") or 1),
        "enabled":   True,
        "added_at":  datetime.now(timezone.utc).isoformat(),
    }
    with _ext_lock:
        _ext_apis[eid] = obj
    _save_ext_apis()
    return redirect("/admin/ext-apis")


@app.route("/admin/ext-apis/<eid>/toggle", methods=["POST"])
@_require_admin
def admin_ext_apis_toggle(eid):
    with _ext_lock:
        if eid in _ext_apis:
            _ext_apis[eid]["enabled"] = not _ext_apis[eid].get("enabled", True)
    _save_ext_apis()
    return redirect("/admin/ext-apis")


@app.route("/admin/ext-apis/<eid>/remove", methods=["POST"])
@_require_admin
def admin_ext_apis_remove(eid):
    with _ext_lock:
        _ext_apis.pop(eid, None)
    _save_ext_apis()
    return redirect("/admin/ext-apis")


@app.route("/admin/ext-apis/<eid>/test")
@_require_admin
def admin_ext_apis_test(eid):
    with _ext_lock:
        conf = _ext_apis.get(eid)
    if not conf:
        return jsonify({"error": "not found"}), 404
    result = _call_ext_api(conf, "@durov")
    return jsonify(result or {"error": "API returned no result"})


# ── Users ────────────────────────────────────────────

@app.route("/admin/users")
@_require_admin
def admin_users():
    page  = int(request.args.get("page", 1))
    search = (request.args.get("q") or "").strip()
    limit = 30
    off   = (page - 1) * limit
    if search:
        rows = botsys.q("SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? OR id=? "
                        "ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                        (f"%{search}%", f"%{search}%",
                         int(search) if search.isdigit() else -1, limit, off), many=True)
        total = len(rows)
    else:
        rows  = botsys.q("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                         (limit, off), many=True)
        total = botsys.q("SELECT COUNT(*) AS n FROM users", one=True)["n"]
    pages = max(1, (total + limit - 1) // limit)
    return render_template_string(ADMIN_HTML, section="users", users=rows,
                                  page=page, pages=pages, total=total, search=search)


@app.route("/admin/users/<int:uid>/ban", methods=["POST"])
@_require_admin
def admin_ban_user(uid):
    botsys.q("UPDATE users SET banned=1 WHERE id=?", (uid,))
    return redirect(request.referrer or "/admin/users")


@app.route("/admin/users/<int:uid>/unban", methods=["POST"])
@_require_admin
def admin_unban_user(uid):
    botsys.q("UPDATE users SET banned=0 WHERE id=?", (uid,))
    return redirect(request.referrer or "/admin/users")


@app.route("/admin/users/<int:uid>/grant-key", methods=["POST"])
@_require_admin
def admin_grant_key(uid):
    days = int(request.form.get("days", 7))
    botsys.grant_key(uid, days, f"Admin {days}d")
    return redirect(request.referrer or "/admin/users")


# ── Keys ─────────────────────────────────────────────

@app.route("/admin/keys")
@_require_admin
def admin_keys():
    page  = int(request.args.get("page", 1))
    limit = 30
    off   = (page - 1) * limit
    now   = int(time.time())
    rows  = botsys.q("SELECT k.*, u.username, u.first_name FROM keys k "
                     "LEFT JOIN users u ON u.id=k.user_id ORDER BY k.created_at DESC LIMIT ? OFFSET ?",
                     (limit, off), many=True)
    total = botsys.q("SELECT COUNT(*) AS n FROM keys", one=True)["n"]
    pages = max(1, (total + limit - 1) // limit)
    return render_template_string(ADMIN_HTML, section="keys", keys=rows,
                                  page=page, pages=pages, total=total, now=now)


@app.route("/admin/keys/generate", methods=["POST"])
@_require_admin
def admin_keys_generate():
    uid  = int(request.form.get("uid", 0))
    days = int(request.form.get("days", 30))
    if uid:
        botsys.grant_key(uid, days, f"Admin {days}d")
    return redirect("/admin/keys")


@app.route("/admin/keys/<key>/revoke", methods=["POST"])
@_require_admin
def admin_keys_revoke(key):
    botsys.q("UPDATE keys SET revoked=1 WHERE key=?", (key,))
    return redirect("/admin/keys")


# ── Channels ─────────────────────────────────────────

@app.route("/admin/channels")
@_require_admin
def admin_channels():
    rows = botsys.list_channels()
    return render_template_string(ADMIN_HTML, section="channels", channels=rows)


@app.route("/admin/channels/add", methods=["POST"])
@_require_admin
def admin_channels_add():
    raw  = (request.form.get("channel") or "").strip()
    link = (request.form.get("link") or "").strip()
    if raw:
        try:
            botsys.adm_add_channel(raw + (" " + link if link else ""))
        except Exception as e:
            pass
    return redirect("/admin/channels")


@app.route("/admin/channels/<int:cid>/remove", methods=["POST"])
@_require_admin
def admin_channels_remove(cid):
    botsys.q("DELETE FROM channels WHERE id=?", (cid,))
    return redirect("/admin/channels")


# ── Settings ─────────────────────────────────────────

@app.route("/admin/settings", methods=["GET", "POST"])
@_require_admin
def admin_settings():
    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "set_limit":
            v = request.form.get("limit", "").strip()
            if v.isdigit():
                botsys.set_setting("daily_limit", int(v))
                msg = "Daily limit updated"
        elif action == "toggle_fj":
            botsys.set_setting("force_join", "0" if botsys.force_join_on() else "1")
            msg = "Force-join toggled"
        elif action == "set_contact":
            botsys.set_setting("contact_username", request.form.get("contact", "").strip())
            msg = "Contact updated"
    return render_template_string(ADMIN_HTML, section="settings",
                                  fj=botsys.force_join_on(),
                                  daily_lim=botsys.daily_limit(),
                                  msg=msg)


# ── DB Export / Import ────────────────────────────────

@app.route("/admin/db/export")
@_require_admin
def admin_db_export():
    db_path = os.environ.get("BOT_DB", "exodus_bot.db")
    return send_file(db_path, as_attachment=True,
                     download_name="tginfo_bot_db.db",
                     mimetype="application/octet-stream")


@app.route("/admin/db/import", methods=["POST"])
@_require_admin
def admin_db_import():
    f = request.files.get("dbfile")
    if not f:
        return redirect("/admin/settings")
    db_path = os.environ.get("BOT_DB", "exodus_bot.db")
    f.save(db_path)
    botsys._db_init()
    return redirect("/admin/dashboard")


# ══════════════════════════════════════════════════════
#  HTML TEMPLATES
# ══════════════════════════════════════════════════════

_CSS = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
a{color:#00d4ff;text-decoration:none}a:hover{text-decoration:underline}
.nav{background:#111;border-bottom:1px solid #222;padding:12px 24px;display:flex;align-items:center;gap:20px}
.nav .brand{font-size:1.2rem;font-weight:700;color:#00d4ff}
.nav a{color:#aaa;font-size:.9rem}
.nav a:hover{color:#00d4ff}
.container{max-width:1100px;margin:0 auto;padding:24px}
.hero{text-align:center;padding:60px 20px 40px}
.hero h1{font-size:2.8rem;font-weight:800;background:linear-gradient(135deg,#00d4ff,#7b2fff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px}
.hero p{color:#888;font-size:1.1rem;margin-bottom:28px}
.btn{display:inline-block;padding:10px 24px;border-radius:8px;font-weight:600;cursor:pointer;border:none;font-size:.95rem;transition:all .2s}
.btn-primary{background:#00d4ff;color:#000}.btn-primary:hover{background:#00b8e0;color:#000}
.btn-success{background:#00c27a;color:#000}.btn-success:hover{background:#00a868}
.btn-danger{background:#ff4757;color:#fff}.btn-danger:hover{background:#e03f4e}
.btn-outline{background:transparent;color:#00d4ff;border:1px solid #00d4ff}
.btn-outline:hover{background:#00d4ff22}
.btn-sm{padding:5px 14px;font-size:.82rem}
.card{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:20px;margin-bottom:16px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:28px}
.stat{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:20px;text-align:center}
.stat .val{font-size:2rem;font-weight:800;color:#00d4ff}
.stat .lbl{color:#666;font-size:.85rem;margin-top:4px}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:32px 0}
.feature{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:24px}
.feature .icon{font-size:2rem;margin-bottom:12px}
.feature h3{color:#fff;margin-bottom:8px}
.feature p{color:#777;font-size:.9rem;line-height:1.5}
code,.code-block{background:#0d0d17;border:1px solid #1e1e2e;border-radius:8px;font-family:monospace;color:#00d4ff}
code{padding:2px 6px;font-size:.9rem}
.code-block{display:block;padding:16px;font-size:.88rem;overflow-x:auto;white-space:pre;color:#c0c0d0;line-height:1.6}
.code-block .kw{color:#7b2fff} .code-block .str{color:#00c27a} .code-block .cm{color:#555}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #1e1e2e;font-size:.9rem}
th{color:#00d4ff;font-weight:600;background:#0d0d17}
tr:hover td{background:#0d0d17}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.78rem;font-weight:600}
.badge-green{background:#00c27a22;color:#00c27a;border:1px solid #00c27a55}
.badge-red{background:#ff475722;color:#ff4757;border:1px solid #ff475755}
.badge-blue{background:#00d4ff22;color:#00d4ff;border:1px solid #00d4ff55}
.badge-yellow{background:#ffa50022;color:#ffa500;border:1px solid #ffa50055}
.badge-purple{background:#7b2fff22;color:#a78bfa;border:1px solid #7b2fff55}
input,select,textarea{background:#0d0d17;border:1px solid #2e2e3e;color:#e0e0e0;padding:9px 14px;border-radius:8px;font-size:.9rem;width:100%}
input:focus,select:focus,textarea:focus{outline:none;border-color:#00d4ff}
.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}
.form-label{font-size:.82rem;color:#888;margin-bottom:4px}
.sidebar{width:200px;flex-shrink:0}
.sidebar a{display:block;padding:10px 14px;border-radius:8px;color:#aaa;font-size:.9rem;margin-bottom:2px;transition:all .2s}
.sidebar a:hover,.sidebar a.active{background:#1e1e2e;color:#00d4ff;text-decoration:none}
.sidebar .section-head{font-size:.75rem;color:#555;text-transform:uppercase;letter-spacing:.05em;padding:14px 14px 6px}
.layout{display:flex;gap:24px;align-items:flex-start}
.main{flex:1;min-width:0}
.page-title{font-size:1.4rem;font-weight:700;margin-bottom:20px;color:#fff}
.alert{padding:10px 16px;border-radius:8px;margin-bottom:16px;font-size:.9rem}
.alert-success{background:#00c27a22;border:1px solid #00c27a55;color:#00c27a}
.alert-danger{background:#ff475722;border:1px solid #ff475755;color:#ff4757}
.progress-bar{background:#1e1e2e;border-radius:20px;height:8px;overflow:hidden}
.progress-fill{height:100%;border-radius:20px;background:linear-gradient(90deg,#00d4ff,#7b2fff);transition:width .3s}
.pill{display:flex;gap:8px;flex-wrap:wrap}
.tag{background:#1e1e2e;color:#aaa;padding:3px 10px;border-radius:20px;font-size:.8rem}
footer{text-align:center;color:#444;padding:40px 20px;font-size:.85rem;border-top:1px solid #1a1a2e;margin-top:60px}
</style>
"""

_NAV_PUBLIC = """
<nav class="nav">
  <span class="brand">⚡ TG Info</span>
  <a href="/">Home</a>
  <a href="/docs">API Docs</a>
  <a href="/check">Check Key</a>
  <a href="https://t.me/{{ bot_username }}" target="_blank">Get Free Key</a>
  <a href="/admin" style="margin-left:auto;color:#7b2fff">Admin</a>
</nav>
"""

LANDING_HTML = _CSS + """
<title>TG Info API — Telegram Lookup</title>
""" + _NAV_PUBLIC + """
<div class="hero">
  <h1>Telegram Info API</h1>
  <p>Resolve any Telegram username or User ID — name, bio, status, DC, and more.</p>
  <a href="https://t.me/{{ bot_username }}" class="btn btn-primary" target="_blank">🤖 Get Free API Key</a>
  &nbsp;
  <a href="/docs" class="btn btn-outline">📖 View API Docs</a>
</div>

<div class="container">
  <div class="stat-grid">
    <div class="stat"><div class="val">{{ total_lookups }}</div><div class="lbl">Total Lookups</div></div>
    <div class="stat"><div class="val">{{ total_success }}</div><div class="lbl">Successful</div></div>
    <div class="stat"><div class="val">{{ ready_accounts }}</div><div class="lbl">Active Sessions</div></div>
    <div class="stat"><div class="val">{{ ext_count }}</div><div class="lbl">External APIs</div></div>
    <div class="stat"><div class="val">{{ uptime }}</div><div class="lbl">Uptime</div></div>
  </div>

  <div class="feature-grid">
    <div class="feature"><div class="icon">👤</div><h3>User Lookup</h3><p>Resolve any @username or numeric ID — name, bio, status, DC, premium badge, and more.</p></div>
    <div class="feature"><div class="icon">📢</div><h3>Channel / Group</h3><p>Get title, member count, description, verification status for any public channel or group.</p></div>
    <div class="feature"><div class="icon">⚡</div><h3>Fast & Reliable</h3><p>Multi-account round-robin with external API fallback. Always returns a result.</p></div>
    <div class="feature"><div class="icon">🔑</div><h3>Free API Keys</h3><p>Refer friends in the Telegram bot to earn free API keys. No credit card needed.</p></div>
  </div>

  <div class="card">
    <div style="font-size:1rem;font-weight:700;color:#fff;margin-bottom:12px">🚀 Quick Example</div>
    <code class="code-block">GET /lookup?key=YOUR_KEY&amp;tg=@durov

{
  "status": true,
  "developer": "@mainexodus",
  "type": "user",
  "id": 5765411204,
  "username": "@durov",
  "first_name": "Pavel",
  "last_name": "Durov",
  "is_verified": true,
  "is_premium": true,
  "bio": "Telegram founder.",
  "dc_id": 4,
  "user_status": "recently",
  "profile_url": "https://t.me/durov"
}</code>
  </div>

  <div class="card">
    <div style="font-size:1rem;font-weight:700;color:#fff;margin-bottom:12px">🔑 How to Get an API Key</div>
    <ol style="color:#888;font-size:.9rem;line-height:2;padding-left:20px">
      <li>Start the bot → <a href="https://t.me/{{ bot_username }}" target="_blank">@{{ bot_username }}</a></li>
      <li>Use your referral link to invite friends</li>
      <li>5 referrals = 7-day key &nbsp;|&nbsp; 10 referrals = 15-day key &nbsp;|&nbsp; 20 referrals = 1-month key</li>
      <li>Or buy a plan directly — contact <a href="https://t.me/mainexodus" target="_blank">@mainexodus</a></li>
    </ol>
    <div style="margin-top:16px">
      <a href="https://t.me/{{ bot_username }}" class="btn btn-success" target="_blank">Get My Key</a>
      &nbsp;
      <a href="/check" class="btn btn-outline">Check Existing Key</a>
    </div>
  </div>
</div>

<footer>Powered by <a href="https://t.me/mainexodus" target="_blank">@mainexodus</a> • TG Info API v1.0</footer>
"""

DOCS_HTML = _CSS + """
<title>API Docs — TG Info</title>
""" + _NAV_PUBLIC + """
<div class="container" style="max-width:860px">
  <div style="padding:32px 0 16px"><h1 style="font-size:2rem;font-weight:800;color:#fff">📖 API Documentation</h1>
  <p style="color:#666;margin-top:6px">Telegram User & Chat Lookup API — v1.0 by @mainexodus</p></div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:12px">Base URL</h2>
    <code>{{ base }}</code>
  </div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:8px">Authentication</h2>
    <p style="color:#888;font-size:.9rem;margin-bottom:12px">Pass your API key as a query parameter or header:</p>
    <code class="code-block">?key=YOUR_KEY
X-API-Key: YOUR_KEY</code>
    <p style="color:#777;font-size:.85rem;margin-top:10px">Get a key from <a href="https://t.me/{{ bot_username }}" target="_blank">@{{ bot_username }}</a></p>
  </div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:12px">Endpoint</h2>
    <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px">
      <span class="badge badge-blue">GET</span>
      <code>/lookup</code>
    </div>
    <table><tr><th>Parameter</th><th>Type</th><th>Required</th><th>Description</th></tr>
    <tr><td><code>key</code></td><td>string</td><td><span class="badge badge-red">required</span></td><td>Your API key</td></tr>
    <tr><td><code>tg</code></td><td>string</td><td><span class="badge badge-red">required</span></td><td>@username <b>or</b> numeric user/chat ID</td></tr>
    </table>

    <div style="margin-top:20px;font-weight:600;color:#fff">Examples</div>
    <code class="code-block">{{ base }}/lookup?key=YOUR_KEY&amp;tg=@username
{{ base }}/lookup?key=YOUR_KEY&amp;tg=7543806069
{{ base }}/key=YOUR_KEY&amp;tg=@username</code>
  </div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:12px">User Response</h2>
    <code class="code-block">{
  "status": true,
  "developer": "@mainexodus",
  "powered_by": "@mainexodus",
  "query": "@username",
  "type": "user",
  "id": 7543806069,
  "username": "@username",
  "first_name": "John",
  "last_name": "Doe",
  "phone": null,
  "bio": "Bio text here",
  "is_bot": false,
  "is_verified": false,
  "is_premium": true,
  "is_scam": false,
  "is_fake": false,
  "is_restricted": false,
  "is_deleted": false,
  "has_photo": true,
  "dc_id": 4,
  "user_status": "recently",
  "profile_url": "https://t.me/username",
  "common_chats": 0
}</code>
  </div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:12px">Channel / Group Response</h2>
    <code class="code-block">{
  "status": true,
  "type": "channel",
  "id": -1001234567890,
  "title": "Channel Name",
  "username": "@channelname",
  "description": "Channel description",
  "members_count": 12500,
  "admins_count": 3,
  "is_verified": false,
  "is_scam": false,
  "has_photo": true,
  "dc_id": 4,
  "profile_url": "https://t.me/channelname"
}</code>
  </div>

  <div class="card">
    <h2 style="color:#00d4ff;margin-bottom:12px">Error Response</h2>
    <code class="code-block">{
  "status": false,
  "developer": "@mainexodus",
  "error": "No user found for @invaliduser"
}</code>
    <table style="margin-top:12px">
    <tr><th>HTTP Code</th><th>Meaning</th></tr>
    <tr><td><code>200</code></td><td>Success</td></tr>
    <tr><td><code>400</code></td><td>Missing or invalid parameters</td></tr>
    <tr><td><code>401</code></td><td>Invalid or missing API key</td></tr>
    <tr><td><code>403</code></td><td>Key revoked or account banned</td></tr>
    <tr><td><code>429</code></td><td>Daily limit reached</td></tr>
    <tr><td><code>503</code></td><td>Service unavailable</td></tr>
    </table>
  </div>
</div>
<footer>Powered by <a href="https://t.me/mainexodus">@mainexodus</a> • TG Info API v1.0</footer>
"""

CHECK_HTML = _CSS + """
<title>Check Key — TG Info</title>
""" + _NAV_PUBLIC + """
<div class="container" style="max-width:600px;padding-top:48px">
  <h1 style="font-size:1.8rem;font-weight:800;color:#fff;margin-bottom:8px">🔑 Check Your Key</h1>
  <p style="color:#666;margin-bottom:28px">Enter your API key to view usage stats and expiry info.</p>

  <form method="post" class="card">
    <div class="form-label">API Key</div>
    <input name="key" value="{{ key }}" placeholder="YOUR-API-KEY" style="margin-bottom:12px">
    <button type="submit" class="btn btn-primary" style="width:100%">Check Key</button>
  </form>

  {% if result is not none %}
    {% if result.found %}
      <div class="card">
        <div style="font-size:1.1rem;font-weight:700;color:#fff;margin-bottom:16px">Key Details</div>
        <table>
          <tr><td style="color:#666">Key</td><td><code>{{ result.key }}</code></td></tr>
          <tr><td style="color:#666">Owner</td><td>{{ result.user }}</td></tr>
          <tr><td style="color:#666">Plan</td><td><span class="badge badge-purple">{{ result.plan }}</span></td></tr>
          <tr><td style="color:#666">Expires</td><td>
            {% if result.expired %}<span class="badge badge-red">EXPIRED</span>
            {% else %}<span class="badge badge-green">{{ result.expires }}</span>{% endif %}
          </td></tr>
          <tr><td style="color:#666">Status</td><td>
            {% if result.revoked %}<span class="badge badge-red">Revoked</span>
            {% else %}<span class="badge badge-green">Active</span>{% endif %}
          </td></tr>
        </table>
        <div style="margin-top:16px">
          <div style="display:flex;justify-content:space-between;font-size:.85rem;color:#666;margin-bottom:6px">
            <span>Today's usage</span><span>{{ result.usage }} / {{ result.limit }}</span>
          </div>
          <div class="progress-bar"><div class="progress-fill" style="width:{{ result.pct }}%"></div></div>
        </div>
      </div>
    {% else %}
      <div class="alert alert-danger">❌ Key not found. Make sure you copied it correctly.</div>
    {% endif %}
  {% endif %}

  <div style="text-align:center;color:#555;font-size:.85rem;margin-top:24px">
    Don't have a key? <a href="https://t.me/{{ bot_username }}" target="_blank">Get one free via the bot</a>
  </div>
</div>
<footer>Powered by <a href="https://t.me/mainexodus">@mainexodus</a></footer>
"""

LOGIN_HTML = _CSS + """
<title>Admin Login — TG Info</title>
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center">
<div class="card" style="width:360px">
  <div style="text-align:center;margin-bottom:24px">
    <div style="font-size:2rem">⚡</div>
    <div style="font-size:1.3rem;font-weight:700;color:#fff">TG Info Admin</div>
    <div style="color:#555;font-size:.85rem">Powered by @mainexodus</div>
  </div>
  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
  <form method="post">
    <div class="form-label">Password</div>
    <input type="password" name="password" placeholder="Enter admin password" style="margin-bottom:14px" autofocus>
    <button type="submit" class="btn btn-primary" style="width:100%">Sign In</button>
  </form>
  <div style="text-align:center;margin-top:16px"><a href="/" style="color:#555;font-size:.85rem">← Back to site</a></div>
</div></div>
"""

_ADMIN_SIDEBAR = """
<div class="sidebar">
  <div class="section-head">Overview</div>
  <a href="/admin/dashboard" class="{{ 'active' if section=='dashboard' else '' }}">📊 Dashboard</a>
  <div class="section-head">Data Sources</div>
  <a href="/admin/accounts" class="{{ 'active' if section=='accounts' else '' }}">🔐 Telethon Accounts</a>
  <a href="/admin/ext-apis" class="{{ 'active' if section=='ext_apis' else '' }}">🌐 External APIs</a>
  <div class="section-head">Users</div>
  <a href="/admin/users" class="{{ 'active' if section=='users' else '' }}">👥 Users</a>
  <a href="/admin/keys" class="{{ 'active' if section=='keys' else '' }}">🔑 API Keys</a>
  <div class="section-head">Bot</div>
  <a href="/admin/channels" class="{{ 'active' if section=='channels' else '' }}">📢 Force-Join</a>
  <a href="/admin/settings" class="{{ 'active' if section=='settings' else '' }}">⚙️ Settings</a>
  <div class="section-head" style="margin-top:auto"></div>
  <a href="/" target="_blank">🌐 View Site</a>
  <a href="/admin/logout" style="color:#ff4757">🚪 Logout</a>
</div>
"""

_ADMIN_HEAD = _CSS + """
<title>Admin — TG Info</title>
<nav class="nav">
  <span class="brand">⚡ TG Info Admin</span>
  <a href="/admin/dashboard">Dashboard</a>
  <span style="margin-left:auto;color:#555;font-size:.85rem">@mainexodus</span>
</nav>
"""

ADMIN_HTML = _ADMIN_HEAD + """
<div class="container">
<div class="layout">
""" + _ADMIN_SIDEBAR + """
<div class="main">

{# ─── DASHBOARD ─── #}
{% if section == 'dashboard' %}
<div class="page-title">📊 Dashboard</div>
<div class="stat-grid">
  <div class="stat"><div class="val">{{ total_lookups }}</div><div class="lbl">Total Lookups</div></div>
  <div class="stat"><div class="val">{{ total_success }}</div><div class="lbl">Successful</div></div>
  <div class="stat"><div class="val">{{ users_count }}</div><div class="lbl">Bot Users</div></div>
  <div class="stat"><div class="val">{{ keys_active }}</div><div class="lbl">Active Keys</div></div>
  <div class="stat"><div class="val">{{ chans }}</div><div class="lbl">Force-Join Channels</div></div>
  <div class="stat"><div class="val">{{ uptime }}</div><div class="lbl">Uptime</div></div>
</div>

<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">🔐 Telethon Accounts</div>
  {% for a in accs %}
  <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #1e1e2e">
    <div style="flex:1">
      <span style="color:#fff;font-weight:600">{{ a.name }}</span>
      <span class="badge {{ 'badge-green' if a.ready else 'badge-red' }}" style="margin-left:8px">{{ 'online' if a.ready else (a.error or 'offline') }}</span>
    </div>
    <div style="color:#555;font-size:.82rem">{{ a.lookup_count }} lookups</div>
  </div>
  {% else %}<div style="color:#555">No accounts configured. <a href="/admin/accounts">Add one</a></div>
  {% endfor %}
</div>

<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">🌐 External APIs</div>
  {% for e in ext %}
  <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #1e1e2e">
    <span style="color:#fff">{{ e.name }}</span>
    <span class="badge {{ 'badge-green' if e.enabled else 'badge-red' }}">{{ 'enabled' if e.enabled else 'disabled' }}</span>
    <span style="color:#555;font-size:.82rem;margin-left:auto">priority {{ e.priority }}</span>
  </div>
  {% else %}<div style="color:#555">No external APIs. <a href="/admin/ext-apis">Add one</a></div>
  {% endfor %}
</div>

{# ─── ACCOUNTS ─── #}
{% elif section == 'accounts' %}
<div class="page-title">🔐 Telethon Accounts</div>
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:14px">➕ Add Account</div>
  <form method="post" action="/admin/accounts/add">
    <div class="form-row">
      <div><div class="form-label">Display Name</div><input name="name" placeholder="My Account"></div>
      <div><div class="form-label">API ID</div><input name="api_id" placeholder="12345678"></div>
      <div><div class="form-label">API Hash</div><input name="api_hash" placeholder="abc123..."></div>
    </div>
    <div><div class="form-label">Session String (from Telethon)</div>
    <textarea name="session" rows="3" placeholder="1BVtsOK8Bu..."></textarea></div>
    <div style="margin-top:12px"><button class="btn btn-success">Add Account</button></div>
  </form>
</div>

{% for a in accs %}
<div class="card">
  <div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap">
    <div style="flex:1">
      <div style="font-weight:700;color:#fff;font-size:1.05rem">{{ a.name }}</div>
      <div style="margin-top:6px">
        <span class="badge {{ 'badge-green' if a.ready else 'badge-red' }}">{{ 'Connected' if a.ready else 'Offline' }}</span>
        {% if a.paused %}<span class="badge badge-yellow" style="margin-left:6px">Paused</span>{% endif %}
      </div>
      {% if a.error %}<div style="color:#ff4757;font-size:.82rem;margin-top:6px">⚠️ {{ a.error }}</div>{% endif %}
      <div style="color:#555;font-size:.82rem;margin-top:8px">
        Lookups: {{ a.lookup_count }} | Success: {{ a.success_count }} | Fail: {{ a.fail_count }}
        {% if a.last_used %} | Last: {{ a.last_used[:16] }}{% endif %}
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:6px">
      <form method="post" action="/admin/accounts/{{ a.id }}/toggle">
        <button class="btn btn-outline btn-sm">{{ 'Resume' if a.paused else 'Pause' }}</button>
      </form>
      <form method="post" action="/admin/accounts/{{ a.id }}/remove" onsubmit="return confirm('Remove account?')">
        <button class="btn btn-danger btn-sm">Remove</button>
      </form>
    </div>
  </div>
  <details style="margin-top:12px">
    <summary style="color:#555;font-size:.85rem;cursor:pointer">Update Session String</summary>
    <form method="post" action="/admin/accounts/{{ a.id }}/update-session" style="margin-top:8px">
      <textarea name="session" rows="2" placeholder="New session string..."></textarea>
      <button class="btn btn-outline btn-sm" style="margin-top:6px">Update</button>
    </form>
  </details>
</div>
{% else %}
<div class="card" style="color:#555">No accounts yet. Add one above.</div>
{% endfor %}

{# ─── EXTERNAL APIS ─── #}
{% elif section == 'ext_apis' %}
<div class="page-title">🌐 External APIs</div>
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:14px">➕ Add External API</div>
  <form method="post" action="/admin/ext-apis/add">
    <div class="form-row">
      <div><div class="form-label">Name</div><input name="name" placeholder="Felix Info API"></div>
      <div><div class="form-label">Base URL</div><input name="base_url" placeholder="https://felix-info-x-bot.onrender.com"></div>
    </div>
    <div class="form-row">
      <div><div class="form-label">Key Parameter</div><input name="key_param" value="key" placeholder="key"></div>
      <div><div class="form-label">API Key Value</div><input name="key_value" placeholder="felix67"></div>
      <div><div class="form-label">TG Parameter</div><input name="tg_param" value="tg" placeholder="tg"></div>
      <div><div class="form-label">Priority (1=first)</div><input name="priority" type="number" value="1" min="1" max="99"></div>
    </div>
    <div style="margin-top:4px;color:#555;font-size:.82rem">
      Will call: <code>BASE_URL?KEY_PARAM=KEY_VALUE&amp;TG_PARAM=@target</code>
    </div>
    <button class="btn btn-success" style="margin-top:12px">Add API</button>
  </form>
</div>

{% for e in ext %}
<div class="card">
  <div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap">
    <div style="flex:1">
      <div style="font-weight:700;color:#fff">{{ e.name }}</div>
      <div style="color:#555;font-size:.82rem;margin-top:6px">{{ e.base_url }}</div>
      <div style="margin-top:8px">
        <span class="badge {{ 'badge-green' if e.enabled else 'badge-red' }}">{{ 'Enabled' if e.enabled else 'Disabled' }}</span>
        <span class="tag" style="margin-left:6px">priority {{ e.priority }}</span>
        <span class="tag">{{ e.key_param }}={{ e.key_value[:4] }}…</span>
        <span class="tag">tg_param={{ e.tg_param }}</span>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:6px">
      <a href="/admin/ext-apis/{{ e.id }}/test" class="btn btn-outline btn-sm" target="_blank">Test</a>
      <form method="post" action="/admin/ext-apis/{{ e.id }}/toggle">
        <button class="btn btn-outline btn-sm">{{ 'Disable' if e.enabled else 'Enable' }}</button>
      </form>
      <form method="post" action="/admin/ext-apis/{{ e.id }}/remove" onsubmit="return confirm('Remove?')">
        <button class="btn btn-danger btn-sm">Remove</button>
      </form>
    </div>
  </div>
</div>
{% else %}<div class="card" style="color:#555">No external APIs configured yet.</div>
{% endfor %}

{# ─── USERS ─── #}
{% elif section == 'users' %}
<div class="page-title">👥 Users ({{ total }})</div>
<form method="get" style="display:flex;gap:8px;margin-bottom:16px">
  <input name="q" value="{{ search }}" placeholder="Search by name / username / ID" style="flex:1">
  <button class="btn btn-primary">Search</button>
</form>
<div class="card" style="padding:0;overflow:hidden">
<table>
  <tr><th>ID</th><th>Name</th><th>Username</th><th>Refs</th><th>Status</th><th>Actions</th></tr>
  {% for u in users %}
  <tr>
    <td><code>{{ u.id }}</code></td>
    <td>{{ u.first_name or '—' }}</td>
    <td>{{ ('@' + u.username) if u.username else '—' }}</td>
    <td>{{ u.total_refs }}</td>
    <td><span class="badge {{ 'badge-red' if u.banned else 'badge-green' }}">{{ 'Banned' if u.banned else 'Active' }}</span></td>
    <td style="display:flex;gap:6px">
      {% if u.banned %}
      <form method="post" action="/admin/users/{{ u.id }}/unban"><button class="btn btn-outline btn-sm">Unban</button></form>
      {% else %}
      <form method="post" action="/admin/users/{{ u.id }}/ban"><button class="btn btn-danger btn-sm">Ban</button></form>
      {% endif %}
      <form method="post" action="/admin/users/{{ u.id }}/grant-key" style="display:flex;gap:4px">
        <input name="days" type="number" value="7" min="1" max="3650" style="width:55px;padding:5px 8px">
        <button class="btn btn-success btn-sm">Key</button>
      </form>
    </td>
  </tr>
  {% else %}<tr><td colspan="6" style="text-align:center;color:#555">No users found</td></tr>
  {% endfor %}
</table></div>
<div style="display:flex;gap:8px;margin-top:12px;justify-content:center">
  {% for p in range(1, pages+1) %}
  <a href="?page={{ p }}&q={{ search }}" class="btn btn-outline btn-sm {{ 'btn-primary' if p==page else '' }}">{{ p }}</a>
  {% endfor %}
</div>

{# ─── KEYS ─── #}
{% elif section == 'keys' %}
<div class="page-title">🔑 API Keys ({{ total }})</div>
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">Generate Key for User</div>
  <form method="post" action="/admin/keys/generate" style="display:flex;gap:8px;flex-wrap:wrap">
    <input name="uid" placeholder="User ID" style="width:160px">
    <input name="days" type="number" value="30" min="1" max="3650" style="width:80px">
    <button class="btn btn-success">Generate</button>
  </form>
</div>
<div class="card" style="padding:0;overflow:hidden">
<table>
  <tr><th>Key</th><th>User</th><th>Plan</th><th>Expires</th><th>Status</th><th>Actions</th></tr>
  {% for k in keys %}
  <tr>
    <td><code style="font-size:.78rem">{{ k.key[:18] }}…</code></td>
    <td>{{ k.first_name or ('ID:'+k.user_id|string) }}</td>
    <td>{{ k.plan or 'Standard' }}</td>
    <td style="font-size:.82rem">
      {% if k.expires_at %}
        {% if k.expires_at < now %}<span style="color:#ff4757">Expired</span>
        {% else %}{{ k.expires_at | int | string | truncate(10,True,'') }}{% endif %}
      {% else %}—{% endif %}
    </td>
    <td><span class="badge {{ 'badge-red' if k.revoked else ('badge-green' if k.expires_at > now else 'badge-yellow') }}">
      {{ 'Revoked' if k.revoked else ('Active' if k.expires_at > now else 'Expired') }}
    </span></td>
    <td>
      {% if not k.revoked %}
      <form method="post" action="/admin/keys/{{ k.key }}/revoke" onsubmit="return confirm('Revoke key?')">
        <button class="btn btn-danger btn-sm">Revoke</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table></div>
<div style="display:flex;gap:8px;margin-top:12px;justify-content:center">
  {% for p in range(1, pages+1) %}
  <a href="?page={{ p }}" class="btn btn-outline btn-sm {{ 'btn-primary' if p==page else '' }}">{{ p }}</a>
  {% endfor %}
</div>

{# ─── CHANNELS ─── #}
{% elif section == 'channels' %}
<div class="page-title">📢 Force-Join Channels</div>
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">➕ Add Channel</div>
  <form method="post" action="/admin/channels/add" style="display:flex;gap:8px;flex-wrap:wrap">
    <input name="channel" placeholder="@username or -100ID" style="flex:1">
    <input name="link" placeholder="Invite link (private channels)" style="flex:1">
    <button class="btn btn-success">Add</button>
  </form>
  <div style="color:#555;font-size:.82rem;margin-top:6px">Bot must be an admin in the channel.</div>
</div>
{% for c in channels %}
<div class="card" style="display:flex;align-items:center;gap:16px">
  <div style="flex:1">
    <div style="font-weight:700;color:#fff">{{ c.title or c.chat_id }}</div>
    <div style="color:#555;font-size:.82rem">{{ c.chat_id }}{% if c.link %} · <a href="{{ c.link }}">invite link</a>{% endif %}</div>
  </div>
  <form method="post" action="/admin/channels/{{ c.id }}/remove" onsubmit="return confirm('Remove?')">
    <button class="btn btn-danger btn-sm">Remove</button>
  </form>
</div>
{% else %}<div class="card" style="color:#555">No channels added yet.</div>
{% endfor %}

{# ─── SETTINGS ─── #}
{% elif section == 'settings' %}
<div class="page-title">⚙️ Settings</div>
{% if msg %}<div class="alert alert-success">✅ {{ msg }}</div>{% endif %}
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">Daily API Limit per Key</div>
  <form method="post" style="display:flex;gap:8px">
    <input type="hidden" name="action" value="set_limit">
    <input name="limit" value="{{ daily_lim }}" type="number" min="1" style="width:120px">
    <button class="btn btn-primary">Update</button>
  </form>
</div>
<div class="card" style="display:flex;align-items:center;gap:16px">
  <div style="flex:1">
    <div style="font-weight:700;color:#fff">Force-Join</div>
    <div style="color:#555;font-size:.85rem">Require users to join channels before using the bot.</div>
  </div>
  <form method="post"><input type="hidden" name="action" value="toggle_fj">
    <button class="btn {{ 'btn-danger' if fj else 'btn-success' }}">{{ 'Disable' if fj else 'Enable' }}</button>
  </form>
</div>
<div class="card">
  <div style="font-weight:700;color:#fff;margin-bottom:12px">🗄️ Database</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <a href="/admin/db/export" class="btn btn-outline">Export DB</a>
    <form method="post" action="/admin/db/import" enctype="multipart/form-data" style="display:flex;gap:8px">
      <input type="file" name="dbfile" accept=".db" style="width:auto">
      <button class="btn btn-outline">Import DB</button>
    </form>
  </div>
</div>
{% endif %}

</div></div></div>
<footer>Powered by <a href="https://t.me/mainexodus">@mainexodus</a> • TG Info Admin</footer>
"""

# ══════════════════════════════════════════════════════
#  FLASK SERVER
# ══════════════════════════════════════════════════════

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


# ══════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        load_accounts()
    except Exception as e:
        print(f"⚠️ Account load error: {e}")

    try:
        _load_ext_apis()
    except Exception as e:
        print(f"⚠️ ExtAPI load error: {e}")

    threading.Thread(target=run_flask, daemon=True).start()
    botsys.install_api_gate(app, DEVELOPER, is_admin=_is_admin)
    botsys.start_bot(PORT)

    print(f"""
{'='*54}
  ⚡ TG INFO API STARTED
  🌐 Site:    http://localhost:{PORT}/
  📖 Docs:    http://localhost:{PORT}/docs
  🔍 Lookup:  http://localhost:{PORT}/lookup?key=K&tg=@user
  🛡  Admin:   http://localhost:{PORT}/admin
  🔑 Pass:    {ADMIN_PASSWORD}
  💾 Accounts: {len(accounts)} | ExtAPIs: {len(_ext_apis)}
{'='*54}
""")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\\n🛑 Shutting down...")
