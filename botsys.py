"""
botsys.py — TG INFO Telegram Bot Frontend
Developer : @Exodus_OWN3R

• Colorful inline keyboard menus
• Force-join system
• Referral → API keys
• API key gate on /lookup
• Full in-bot admin panel
• DB export / import via bot
"""

import os, re, json, time, html, sqlite3, secrets, threading, copy
from concurrent.futures import ThreadPoolExecutor
import requests

# ══════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════
DB_PATH          = os.environ.get("BOT_DB", "tginfo_bot.db")
DEV_TAG          = "Exodus OWN3R" 
DEV_URL          = "https://t.me/Exodus_OWN3R" 
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "https://t.me/Exodus_OWN3R").lstrip("@")
CONTACT_URL      = f"https://t.me/{CONTACT_USERNAME}"
DEFAULT_DAILY    = int(os.environ.get("API_DAILY_LIMIT", "300"))

PLANS = [(5, 7, "7 Days"), (10, 15, "15 Days"), (20, 30, "1 Month")]

INTERNAL_TOKEN = secrets.token_hex(24)

FOOTER = f"\n\n━━━━━━━━━━━━━━━━━━\n💫 <b>Powered by:</b> {DEV_TAG}"

BOT_TOKEN    = ""
BOT_ID       = 0
BOT_USERNAME = ""
BASE_URL     = ""
LOCAL_PORT   = 5000
ADMIN_IDS    = set()

E = html.escape

# ══════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at INTEGER,
  referred_by INTEGER, ref_counted INTEGER DEFAULT 0,
  ref_points INTEGER DEFAULT 0, total_refs INTEGER DEFAULT 0,
  banned INTEGER DEFAULT 0, lookup_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT UNIQUE, title TEXT, link TEXT, added_at INTEGER);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY, user_id INTEGER, created_at INTEGER,
  expires_at INTEGER, revoked INTEGER DEFAULT 0,
  daily_limit INTEGER, plan TEXT);
CREATE TABLE IF NOT EXISTS usage(
  key TEXT, day TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(key,day));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

_db     = None
_dblock = threading.RLock()


def _db_init():
    global _db
    _db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _db.row_factory = sqlite3.Row
    try:
        _db.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    _db.executescript(SCHEMA)


def q(sql, args=(), one=False, many=False):
    with _dblock:
        if _db is None:
            _db_init()
        cur = _db.execute(sql, args)
        if one:
            return cur.fetchone()
        if many:
            return cur.fetchall()
        return cur.rowcount


def get_setting(k, default=None):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else default


def set_setting(k, v):
    q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def daily_limit():
    try:
        return int(get_setting("daily_limit", DEFAULT_DAILY))
    except:
        return DEFAULT_DAILY


def force_join_on():
    return get_setting("force_join", "1") == "1"


# ── users ────────────────────────────────────────────
def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def upsert_user(frm, referred_by=None):
    uid   = frm["id"]
    uname = frm.get("username") or ""
    fname = frm.get("first_name") or ""
    row   = get_user(uid)
    if row is None:
        rb = None
        if referred_by and referred_by != uid and get_user(referred_by):
            rb = referred_by
        q("INSERT INTO users(id,username,first_name,joined_at,referred_by) VALUES(?,?,?,?,?)",
          (uid, uname, fname, int(time.time()), rb))
        return get_user(uid), True
    q("UPDATE users SET username=?, first_name=? WHERE id=?", (uname, fname, uid))
    return row, False


# ── keys ────────────────────────────────────────────
def _new_key():
    h = secrets.token_hex(12).upper()
    return f"TGI-{h[:8]}-{h[8:16]}-{h[16:]}"


def active_key(uid):
    return q("SELECT * FROM keys WHERE user_id=? AND revoked=0 AND expires_at>? "
             "ORDER BY expires_at DESC LIMIT 1", (uid, int(time.time())), one=True)


def grant_key(uid, days, label=""):
    now = int(time.time())
    row = active_key(uid)
    if row:
        exp = max(now, row["expires_at"]) + days * 86400
        q("UPDATE keys SET expires_at=? WHERE key=?", (exp, row["key"]))
        return row["key"], exp
    key, exp = _new_key(), now + days * 86400
    q("INSERT INTO keys(key,user_id,created_at,expires_at,plan) VALUES(?,?,?,?,?)",
      (key, uid, now, exp, label))
    return key, exp


def claim_plan(uid, idx):
    cost, days, label = PLANS[idx]
    n = q("UPDATE users SET ref_points=ref_points-? WHERE id=? AND ref_points>=?", (cost, uid, cost))
    if not n:
        return None
    return grant_key(uid, days, label)


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def key_usage_today(key):
    r = q("SELECT count FROM usage WHERE key=? AND day=?", (key, _today()), one=True)
    return r["count"] if r else 0


def validate_and_count(key):
    if not key:
        return False, 401, "API key required. Get a free key from the bot."
    row = q("SELECT k.*, u.banned AS banned FROM keys k LEFT JOIN users u ON u.id=k.user_id "
            "WHERE k.key=?", (key,), one=True)
    if not row:
        return False, 401, "Invalid API key."
    if row["revoked"]:
        return False, 403, "API key has been revoked."
    if row["banned"]:
        return False, 403, "Account is banned."
    if row["expires_at"] <= int(time.time()):
        return False, 401, "API key expired. Refer more friends or buy a plan."
    limit = row["daily_limit"] or daily_limit()
    day   = _today()
    q("INSERT OR IGNORE INTO usage(key,day,count) VALUES(?,?,0)", (key, day))
    if not q("UPDATE usage SET count=count+1 WHERE key=? AND day=? AND count<?", (key, day, limit)):
        return False, 429, f"Daily limit reached ({limit}/day)."
    return True, 200, "ok"


# ── channels ────────────────────────────────────────
def list_channels():
    return q("SELECT * FROM channels ORDER BY id", many=True)


def adm_add_channel(raw):
    """Parse '@username [link]' or '-100ID [link]' and insert."""
    parts = raw.strip().split(None, 1)
    cid   = parts[0].strip()
    link  = parts[1].strip() if len(parts) > 1 else ""
    try:
        info = tg("getChat", chat_id=cid)
    except TGError as e:
        raise ValueError(f"Cannot get chat info: {e.desc}")
    title = info.get("title") or info.get("username") or str(cid)
    chat_id_str = str(info.get("id", cid))
    if not link and info.get("invite_link"):
        link = info["invite_link"]
    q("INSERT OR IGNORE INTO channels(chat_id,title,link,added_at) VALUES(?,?,?,?)",
      (chat_id_str, title, link, int(time.time())))
    return q("SELECT * FROM channels WHERE chat_id=?", (chat_id_str,), one=True)


# ══════════════════════════════════════════════════════
#  TELEGRAM API LAYER
# ══════════════════════════════════════════════════════
class TGError(Exception):
    def __init__(self, desc, code=0, retry_after=0):
        super().__init__(desc)
        self.desc, self.code, self.retry_after = desc, code, retry_after


_tl = threading.local()


def tg(method, _t=30, **params):
    s = getattr(_tl, "s", None)
    if s is None:
        s = _tl.s = requests.Session()
    try:
        r    = s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=_t)
        data = r.json()
    except Exception as e:
        raise TGError(f"network: {e}")
    if not data.get("ok"):
        raise TGError(data.get("description", "error"), data.get("error_code", 0),
                      (data.get("parameters") or {}).get("retry_after", 0))
    return data["result"]


def _strip_style(markup):
    m = copy.deepcopy(markup)
    for row in m.get("inline_keyboard", []):
        for b in row:
            b.pop("style", None)
    return m


def _call_markup(method, **params):
    try:
        return tg(method, **params)
    except TGError as e:
        if "message is not modified" in e.desc:
            return None
        if e.code == 400 and params.get("reply_markup"):
            low = e.desc.lower()
            if "style" in low or "button" in low or "unsupported" in low:
                params["reply_markup"] = _strip_style(params["reply_markup"])
                try:
                    return tg(method, **params)
                except TGError as e2:
                    if "message is not modified" in e2.desc:
                        return None
                    raise
        raise


def send(cid, text, markup=None):
    p = dict(chat_id=cid, text=text, parse_mode="HTML",
             link_preview_options={"is_disabled": True})
    if markup:
        p["reply_markup"] = markup
    return _call_markup("sendMessage", **p)


def edit(cid, mid, text, markup=None):
    p = dict(chat_id=cid, message_id=mid, text=text, parse_mode="HTML",
             link_preview_options={"is_disabled": True})
    if markup:
        p["reply_markup"] = markup
    return _call_markup("editMessageText", **p)


def show(cid, mid, text, markup=None):
    if mid:
        try:
            return edit(cid, mid, text, markup)
        except TGError:
            pass
    return send(cid, text, markup)


def B(text, cb=None, url=None, style=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = cb or "noop"
    if style:
        b["style"] = style
    return b


def KB(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


# ══════════════════════════════════════════════════════
#  FORCE JOIN
# ══════════════════════════════════════════════════════
_member_cache = {}
_CACHE_TTL    = 60


def _is_member(uid, chat_id):
    ck = (uid, chat_id)
    if time.time() - _member_cache.get(ck, 0) < _CACHE_TTL:
        return True
    try:
        m  = tg("getChatMember", chat_id=chat_id, user_id=uid)
    except TGError as e:
        print(f"[ForceJoin] {chat_id}: {e.desc}", flush=True)
        return True
    st = m.get("status")
    ok = st in ("creator", "administrator", "member") or (
         st == "restricted" and m.get("is_member", False))
    if ok:
        _member_cache[ck] = time.time()
    return ok


def missing_channels(uid):
    if uid in ADMIN_IDS or not force_join_on():
        return []
    return [c for c in list_channels() if not _is_member(uid, c["chat_id"])]


_pending_target = {}


def join_prompt(cid, mid, miss):
    lines = "\n".join(f"  📣 <b>{E(c['title'] or 'Channel')}</b>" for c in miss)
    text  = ("<b>🔒 JOIN REQUIRED</b>\n━━━━━━━━━━━━━━━━━━\n"
             "Join these channels to use the bot:\n\n"
             f"{lines}\n\n"
             "<blockquote>After joining, tap <b>✅ Verify</b> to continue.</blockquote>"
             + FOOTER)
    rows  = []
    for c in miss:
        if c["link"]:
            rows.append([B(f"📣 {c['title'] or 'Join Channel'}", url=c["link"], style="primary")])
    rows.append([B("✅ Verify Membership", cb="verify", style="success")])
    rows.append([B("💎 Buy Key Directly", url=CONTACT_URL, style="danger")])
    show(cid, mid, text, KB(*rows))


def gate(uid, cid, mid=None, pending=None):
    miss = missing_channels(uid)
    if not miss:
        return True
    if pending:
        _pending_target[uid] = pending
    join_prompt(cid, mid, miss)
    return False


# ══════════════════════════════════════════════════════
#  REFERRALS
# ══════════════════════════════════════════════════════
def try_count_referral(uid):
    row = get_user(uid)
    if not row or not row["referred_by"] or row["ref_counted"]:
        return
    if missing_channels(uid):
        return
    if not q("UPDATE users SET ref_counted=1 WHERE id=? AND ref_counted=0", (uid,)):
        return
    ref = row["referred_by"]
    q("UPDATE users SET ref_points=ref_points+1, total_refs=total_refs+1 WHERE id=?", (ref,))
    r = get_user(ref)
    if r:
        name = E(row["first_name"] or "Someone")
        try:
            send(ref,
                 f"<b>🎉 NEW REFERRAL!</b>\n"
                 f"<blockquote>{name} joined via your link!\n"
                 f"🎁 Your referral points: <b>{r['ref_points'] + 1}</b></blockquote>" + FOOTER,
                 KB([B("🎁 View Refer & Earn", cb="refer", style="success"),
                     B("🔑 My Key", cb="mykey", style="primary")]))
        except TGError:
            pass


# ══════════════════════════════════════════════════════
#  LOCAL LOOKUP CALL
# ══════════════════════════════════════════════════════
def call_lookup(target):
    try:
        r = requests.get(
            f"http://localhost:{LOCAL_PORT}/lookup",
            params={"tg": target},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=25,
        )
        return r.json()
    except Exception as e:
        return {"status": False, "error": str(e)}


# ══════════════════════════════════════════════════════
#  RESULT FORMATTER
# ══════════════════════════════════════════════════════
def fmt_lookup_result(data, query):
    if not data.get("status"):
        err = E(str(data.get("error", "Unknown error")))
        return (f"<b>❌ LOOKUP FAILED</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"Query: <code>{E(str(query))}</code>\n\n"
                f"<blockquote>{err}</blockquote>" + FOOTER)

    t    = data.get("type", "unknown")
    icon = {"user": "👤", "bot": "🤖", "channel": "📢", "supergroup": "👥", "group": "👥"}.get(t, "🔍")

    lines = [f"<b>{icon} TELEGRAM {t.upper()} INFO</b>", "━━━━━━━━━━━━━━━━━━"]

    def row(label, val, code=False):
        if val is None or val == "":
            return
        v = f"<code>{E(str(val))}</code>" if code else f"<b>{E(str(val))}</b>"
        lines.append(f"{label}: {v}")

    if t in ("user", "bot"):
        name_parts = []
        if data.get("first_name"): name_parts.append(data["first_name"])
        if data.get("last_name"):  name_parts.append(data["last_name"])
        full_name = " ".join(name_parts) or "—"

        row("👤 Name",    full_name)
        row("🆔 User ID", data.get("id"), code=True)
        row("📛 Username", data.get("username"))
        row("📱 Phone",   data.get("phone"), code=True)
        row("📝 Bio",     data.get("bio"))

        flags = []
        if data.get("is_bot"):        flags.append("🤖 Bot")
        if data.get("is_verified"):   flags.append("✅ Verified")
        if data.get("is_premium"):    flags.append("💎 Premium")
        if data.get("is_scam"):       flags.append("⚠️ Scam")
        if data.get("is_fake"):       flags.append("🚫 Fake")
        if data.get("is_restricted"): flags.append("🔒 Restricted")
        if data.get("is_deleted"):    flags.append("🗑 Deleted")
        if flags:
            lines.append(f"🏷 Flags: {' | '.join(flags)}")

        status_map = {
            "online":     "🟢 Online",
            "recently":   "🕐 Recently",
            "last_week":  "📅 Last Week",
            "last_month": "📅 Last Month",
            "offline":    "⚫ Offline",
            "unknown":    "❔ Hidden",
        }
        row("🟢 Status", status_map.get(data.get("user_status", ""), "—"))
        row("📡 DC",     data.get("dc_id"), code=True)
        row("🖼 Photo",  "Yes" if data.get("has_photo") else "No")
        row("🤝 Common Chats", data.get("common_chats"))
        row("🔗 Profile", data.get("profile_url"))

    elif t in ("channel", "supergroup", "group"):
        row("📌 Title",       data.get("title"))
        row("🆔 Chat ID",     data.get("id"), code=True)
        row("📛 Username",    data.get("username"))
        row("📝 Description", data.get("description"))
        row("👥 Members",     data.get("members_count"))
        row("👮 Admins",      data.get("admins_count"))
        row("🚫 Banned",      data.get("banned_count"))

        flags = []
        if data.get("is_verified"):   flags.append("✅ Verified")
        if data.get("is_scam"):       flags.append("⚠️ Scam")
        if data.get("is_fake"):       flags.append("🚫 Fake")
        if data.get("is_restricted"): flags.append("🔒 Restricted")
        if flags:
            lines.append(f"🏷 Flags: {' | '.join(flags)}")

        row("📡 DC",      data.get("dc_id"), code=True)
        row("🖼 Photo",   "Yes" if data.get("has_photo") else "No")
        row("🔗 Profile", data.get("profile_url"))

    src = data.get("fallback_source") or data.get("_source")
    if src:
        lines.append(f"\n<i>Source: {E(str(src))}</i>")

    lines.append(FOOTER)
    return "\n".join(lines)


def lookup_markup(query):
    encoded = E(str(query))
    return KB(
        [B("🔍 Lookup Again", cb="noop", style="primary"),
         B("🌐 Open Profile", url=f"https://t.me/{str(query).lstrip('@')}", style="success")],
        [B("🏠 Menu", cb="menu", style="primary"),
         B("🔑 My Key", cb="mykey", style="success")],
    )


# ══════════════════════════════════════════════════════
#  SCREENS
# ══════════════════════════════════════════════════════
def fmt_exp(ts):
    left = max(0, ts - int(time.time()))
    d, h = left // 86400, (left % 86400) // 3600
    return f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))} UTC  ({d}d {h}h left)"


def menu_markup(uid):
    rows = [
        [B("🔍 Lookup Username", cb="howto", style="primary"),
         B("🆔 Lookup by ID",    cb="howto", style="primary")],
        [B("🎁 Refer & Earn",    cb="refer", style="success"),
         B("🔑 My API Key",      cb="mykey", style="success")],
        [B("📖 API Docs",        cb="docs",  style="primary"),
         B("🌐 Website",         url=BASE_URL or DEV_URL, style="primary")],
        [B("💎 Buy Premium",     url=CONTACT_URL, style="danger"),
         B("👨‍💻 Developer",       url=DEV_URL, style="success")],
    ]
    if uid in ADMIN_IDS:
        rows.append([B("🛡️ Admin Panel", cb="adm:home", style="danger")])
    return KB(*rows)


def screen_menu(uid, name):
    text = (f"<b>⚡ TG INFO BOT</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Hey <b>{E(name or 'there')}</b> 👋\n\n"
            "Send me any <b>@username</b> or <b>User/Chat ID</b> and I'll return all available info instantly.\n\n"
            "<blockquote>"
            "👤 User info — name, bio, status, DC, flags\n"
            "📢 Channel info — title, members, description\n"
            "🎁 Refer friends → earn free API keys\n"
            f"💎 Premium plans → <a href='{CONTACT_URL}'>contact us</a>"
            "</blockquote>"
            + FOOTER)
    return text, menu_markup(uid)


def screen_howto(uid):
    text = ("<b>🔍 HOW TO LOOKUP</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            "Just send one of these:\n\n"
            "<blockquote>"
            "📛 <b>By Username:</b>\n"
            "<code>@username</code>\n"
            "<code>username</code>\n\n"
            "🆔 <b>By User/Chat ID:</b>\n"
            "<code>7543806069</code>\n"
            "<code>-1001234567890</code>\n\n"
            "💡 Works for users, bots, channels and groups!"
            "</blockquote>" + FOOTER)
    return text, KB(
        [B("🏠 Back to Menu", cb="menu", style="primary"),
         B("🎁 Refer & Earn", cb="refer", style="success")])


def key_card(key, exp, plan):
    text = (f"<b>🔑 YOUR API KEY</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{key}</code>\n\n"
            f"📋 Plan: <b>{E(plan)}</b>\n"
            f"⏳ Expires: <b>{fmt_exp(exp)}</b>\n\n"
            "<b>Usage:</b>\n"
            f"<code>GET /lookup?key={key}&amp;tg=@username</code>\n\n"
            f"📖 Full docs: <a href='{BASE_URL}/docs'>API Documentation</a>" + FOOTER)
    return text, KB(
        [B("📖 API Docs",   url=f"{BASE_URL}/docs", style="primary"),
         B("✅ Check Key",  url=f"{BASE_URL}/check", style="success")],
        [B("🏠 Menu",       cb="menu", style="primary")])


def screen_key(uid):
    row = active_key(uid)
    if not row:
        text = ("<b>🔑 NO ACTIVE KEY</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                "You don't have an active API key.\n\n"
                "<b>How to get one for free:</b>\n"
                "1️⃣ Refer <b>5 friends</b> → 7-day key\n"
                "2️⃣ Refer <b>10 friends</b> → 15-day key\n"
                "3️⃣ Refer <b>20 friends</b> → 1-month key\n\n"
                f"💎 Or buy directly from <a href='{CONTACT_URL}'>@{E(CONTACT_USERNAME)}</a>"
                + FOOTER)
        return text, KB(
            [B("🎁 Get Referral Link", cb="refer", style="success")],
            [B("💎 Buy Premium",       url=CONTACT_URL, style="danger")],
            [B("🏠 Menu",              cb="menu", style="primary")])
    usage = key_usage_today(row["key"])
    lim   = row["daily_limit"] or daily_limit()
    pct   = min(100, int(usage / lim * 100)) if lim else 0
    bar   = "█" * (pct // 10) + "░" * (10 - pct // 10)

    text  = (f"<b>🔑 YOUR API KEY</b>\n━━━━━━━━━━━━━━━━━━\n\n"
             f"<code>{row['key']}</code>\n\n"
             f"📋 Plan: <b>{row['plan'] or 'Standard'}</b>\n"
             f"⏳ Expires: <b>{fmt_exp(row['expires_at'])}</b>\n\n"
             f"📊 Today: <b>{usage}/{lim}</b>  [{bar}]\n\n"
             "<b>Quick use:</b>\n"
             f"<code>/lookup?key={row['key']}&amp;tg=@username</code>"
             + FOOTER)
    return text, KB(
        [B("📖 API Docs",  url=f"{BASE_URL}/docs", style="primary"),
         B("✅ Check Key", url=f"{BASE_URL}/check", style="success")],
        [B("🎁 Refer & Earn", cb="refer", style="success"),
         B("🏠 Menu",         cb="menu",  style="primary")])


def screen_refer(uid):
    row  = get_user(uid)
    pts  = row["ref_points"]   if row else 0
    refs = row["total_refs"]   if row else 0
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else "Bot not configured"

    plans_text = "\n".join(f"  {'✅' if pts >= c else '🔒'} {c} refs → <b>{l}</b>" for c, d, l in PLANS)

    text = (f"<b>🎁 REFER & EARN</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Your referral link:\n<code>{link}</code>\n\n"
            f"👥 Total referrals: <b>{refs}</b>\n"
            f"🎯 Spendable points: <b>{pts}</b>\n\n"
            "<b>Rewards:</b>\n"
            f"{plans_text}" + FOOTER)

    rows = []
    for i, (cost, days, label) in enumerate(PLANS):
        if pts >= cost:
            rows.append([B(f"🎁 Claim {label} ({cost} pts)", cb=f"claim:{i}", style="success")])
    rows.append([B("🔑 My Key", cb="mykey", style="primary"),
                 B("🏠 Menu",   cb="menu",  style="primary")])
    return text, KB(*rows)


def screen_docs(uid):
    base = BASE_URL or f"http://localhost:{LOCAL_PORT}"
    text = (f"<b>📖 API DOCUMENTATION</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Endpoint:</b>\n"
            f"<code>GET {base}/lookup</code>\n\n"
            "<b>Parameters:</b>\n"
            "• <code>key</code> — Your API key\n"
            "• <code>tg</code>  — @username or numeric ID\n\n"
            "<b>Examples:</b>\n"
            f"<code>{base}/lookup?key=YOUR_KEY&amp;tg=@durov</code>\n"
            f"<code>{base}/lookup?key=YOUR_KEY&amp;tg=5765411204</code>\n"
            f"<code>{base}/key=YOUR_KEY&amp;tg=@durov</code>\n\n"
            "<b>Response fields:</b>\n"
            "id, username, first_name, last_name, bio,\n"
            "is_bot, is_verified, is_premium, is_scam,\n"
            "dc_id, user_status, profile_url, ..."
            + FOOTER)
    return text, KB(
        [B("📄 Full Docs",  url=f"{base}/docs",  style="primary"),
         B("✅ Check Key",  url=f"{base}/check", style="success")],
        [B("🔑 My Key", cb="mykey", style="success"),
         B("🏠 Menu",   cb="menu",  style="primary")])


# ══════════════════════════════════════════════════════
#  ADMIN PANEL SCREENS
# ══════════════════════════════════════════════════════
admin_state = {}


def adm_home():
    users  = q("SELECT COUNT(*) AS n FROM users", one=True)["n"]
    keys   = q("SELECT COUNT(*) AS n FROM keys WHERE revoked=0 AND expires_at>?",
               (int(time.time()),), one=True)["n"]
    banned = q("SELECT COUNT(*) AS n FROM users WHERE banned=1", one=True)["n"]
    chans  = q("SELECT COUNT(*) AS n FROM channels", one=True)["n"]
    text   = (f"<b>🛡️ ADMIN PANEL</b>\n━━━━━━━━━━━━━━━━━━\n\n"
              f"👥 Users: <b>{users}</b>  (🚫 {banned} banned)\n"
              f"🔑 Active Keys: <b>{keys}</b>\n"
              f"📢 Force-Join Channels: <b>{chans}</b>\n"
              f"🔒 Force-Join: <b>{'ON ✅' if force_join_on() else 'OFF ❌'}</b>\n"
              f"📊 Daily Limit: <b>{daily_limit()}/key</b>"
              + FOOTER)
    return text, KB(
        [B("👥 Users",      cb="adm:users",  style="primary"),
         B("🔑 Keys",       cb="adm:keys",   style="success")],
        [B("📢 Channels",   cb="adm:ch",     style="primary"),
         B("⚙️ Settings",   cb="adm:set",    style="primary")],
        [B("📣 Broadcast",  cb="adm:bc",     style="danger")],
        [B("🌐 Web Panel",  url=f"{BASE_URL}/admin", style="success")],
        [B("🏠 Close",      cb="menu",        style="primary")])


def adm_users():
    rows  = q("SELECT * FROM users ORDER BY joined_at DESC LIMIT 20", many=True)
    total = q("SELECT COUNT(*) AS n FROM users", one=True)["n"]
    lines = [f"<b>👥 USERS</b> (total: {total})\n━━━━━━━━━━━━━━━━━━"]
    for u in rows:
        flag = "🚫" if u["banned"] else "✅"
        name = E(u["first_name"] or "—")
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(f"{flag} <code>{u['id']}</code> {name} {uname} | refs:{u['total_refs']}")
    return "\n".join(lines) + FOOTER, KB(
        [B("🔍 Find User",  cb="adm:ufind", style="primary"),
         B("🔙 Back",       cb="adm:home",  style="danger")])


def adm_user_card(uid):
    u = get_user(uid)
    if not u:
        return "❌ User not found.", KB([B("🔙 Back", cb="adm:users", style="primary")])
    k   = active_key(uid)
    key_info = f"\n🔑 Key: <code>{k['key'][:20]}…</code> ({fmt_exp(k['expires_at'])})" if k else "\n🔑 No active key"
    text = (f"<b>👤 USER CARD</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"ID: <code>{u['id']}</code>\n"
            f"Name: {E(u['first_name'] or '—')}\n"
            f"Username: {'@' + u['username'] if u['username'] else '—'}\n"
            f"Status: {'🚫 Banned' if u['banned'] else '✅ Active'}\n"
            f"Referrals: {u['total_refs']} | Points: {u['ref_points']}\n"
            f"{key_info}" + FOOTER)
    return text, KB(
        [B("🚫 Ban" if not u["banned"] else "✅ Unban",
           cb=f"adm:{'uban' if not u['banned'] else 'uunban'}:{uid}",
           style="danger" if not u["banned"] else "success")],
        [B("🔑 Grant 7d",  cb=f"adm:ukey:{uid}:7",   style="success"),
         B("🔑 Grant 30d", cb=f"adm:ukey:{uid}:30",  style="success")],
        [B("🗑 Revoke Keys", cb=f"adm:urev:{uid}",   style="danger"),
         B("🔙 Back",        cb="adm:users",          style="primary")])


def adm_keys():
    rows  = q("SELECT k.key, k.plan, k.expires_at, k.revoked, u.username, u.first_name "
              "FROM keys k LEFT JOIN users u ON u.id=k.user_id "
              "ORDER BY k.created_at DESC LIMIT 15", many=True)
    now   = int(time.time())
    lines = ["<b>🔑 RECENT KEYS</b>\n━━━━━━━━━━━━━━━━━━"]
    for k in rows:
        status = "🗑" if k["revoked"] else ("✅" if k["expires_at"] > now else "⏰")
        uname  = f"@{k['username']}" if k["username"] else (k["first_name"] or "—")
        lines.append(f"{status} <code>{k['key'][:16]}…</code> {E(str(uname))} | {k['plan'] or 'Std'}")
    return "\n".join(lines) + FOOTER, KB(
        [B("➕ Generate Key", cb="adm:kgen", style="success"),
         B("🗑 Revoke Key",   cb="adm:krev", style="danger")],
        [B("🔙 Back",         cb="adm:home", style="primary")])


def adm_channels():
    rows  = list_channels()
    lines = ["<b>📢 FORCE-JOIN CHANNELS</b>\n━━━━━━━━━━━━━━━━━━"]
    for c in rows:
        lines.append(f"• {E(c['title'] or c['chat_id'])}")
    if not rows:
        lines.append("No channels configured.")
    return "\n".join(lines) + FOOTER, KB(
        [B("➕ Add Channel",  cb="adm:chadd", style="success"),
         B("🗑 Remove",       cb="adm:chdel", style="danger")],
        [B("🔙 Back",         cb="adm:home",  style="primary")])


def adm_settings():
    text = ("<b>⚙️ SETTINGS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🔒 Force-join: <b>{'ON ✅' if force_join_on() else 'OFF ❌'}</b>\n"
            f"📊 Daily limit / key: <b>{daily_limit()}</b>\n"
            f"💬 Contact: <b>@{E(CONTACT_USERNAME)}</b>\n"
            f"🌐 Base URL: <code>{E(BASE_URL)}</code>\n\n"
            "<b>🏆 Referral plans:</b>\n"
            + "\n".join(f"  • {c} refs → <b>{l}</b>" for c, d, l in PLANS))
    return text, KB(
        [B("🔒 Toggle Force-Join",  cb="adm:settog",  style="primary"),
         B("📊 Set Daily Limit",    cb="adm:setlim",  style="success")],
        [B("🌐 Open Web Admin",     url=f"{BASE_URL}/admin", style="danger")],
        [B("🔙 Back",               cb="adm:home",    style="primary")])


# ══════════════════════════════════════════════════════
#  ADMIN CALLBACKS
# ══════════════════════════════════════════════════════
def on_admin_callback(uid, cid, mid, data, ack):
    parts = data.split(":")
    act   = parts[1] if len(parts) > 1 else "home"
    prev  = admin_state.pop(uid, None)

    if act == "home":   return show(cid, mid, *adm_home())
    if act == "stats":  return show(cid, mid, *adm_home())
    if act == "ch":     return show(cid, mid, *adm_channels())
    if act == "users":  return show(cid, mid, *adm_users())
    if act == "keys":   return show(cid, mid, *adm_keys())
    if act == "set":    return show(cid, mid, *adm_settings())

    if act == "chadd":
        admin_state[uid] = {"mode": "add_channel"}
        return show(cid, mid,
                    "<b>➕ ADD CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\n"
                    "Send: <code>@username</code>  or  <code>-100ID https://t.me/+invite</code>\n"
                    "<i>Bot must be admin there.</i>",
                    KB([B("❌ Cancel", cb="adm:ch", style="danger")]))

    if act == "chdel":
        admin_state[uid] = {"mode": "del_channel"}
        rows  = list_channels()
        lines = [f"{c['id']}. {c['title'] or c['chat_id']}" for c in rows]
        return show(cid, mid,
                    "<b>🗑 REMOVE CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\nSend the channel number:\n\n" +
                    "\n".join(lines) or "No channels.",
                    KB([B("❌ Cancel", cb="adm:ch", style="danger")]))

    if act == "bc":
        admin_state[uid] = {"mode": "broadcast"}
        return show(cid, mid,
                    "<b>📣 BROADCAST</b>\n━━━━━━━━━━━━━━━━━━\nSend your message (HTML ok).",
                    KB([B("❌ Cancel", cb="adm:home", style="danger")]))

    if act == "bcgo":
        txt = (prev or {}).get("text")
        if not txt:
            return ack("Nothing to send", True)
        ack("Broadcasting…")
        threading.Thread(target=_broadcast, args=(cid, txt), daemon=True).start()
        return show(cid, mid, "<b>📣 Broadcasting…</b> You'll get a report when done.")

    if act == "ufind":
        admin_state[uid] = {"mode": "find_user"}
        return show(cid, mid, "<b>🔍 FIND USER</b>\nSend the numeric Telegram ID.",
                    KB([B("❌ Cancel", cb="adm:users", style="danger")]))

    if act in ("uban", "uunban"):
        t = int(parts[2])
        q("UPDATE users SET banned=? WHERE id=?", (1 if act == "uban" else 0, t))
        ack("Done")
        return show(cid, mid, *adm_user_card(t))

    if act == "urev":
        t = int(parts[2])
        q("UPDATE keys SET revoked=1 WHERE user_id=?", (t,))
        ack("Keys revoked")
        return show(cid, mid, *adm_user_card(t))

    if act == "ukey":
        t, days = int(parts[2]), int(parts[3])
        k, exp  = grant_key(t, days, f"Admin {days}d")
        ack(f"Key granted: {k[:12]}…")
        try:
            send(t, f"<b>🎁 KEY GRANTED!</b>\n<code>{k}</code>\nExpires: {fmt_exp(exp)}" + FOOTER)
        except:
            pass
        return show(cid, mid, *adm_user_card(t))

    if act == "kgen":
        admin_state[uid] = {"mode": "gen_key"}
        return show(cid, mid,
                    "<b>➕ GENERATE KEY</b>\nSend: <code>USER_ID DAYS</code>",
                    KB([B("❌ Cancel", cb="adm:keys", style="danger")]))

    if act == "krev":
        admin_state[uid] = {"mode": "revoke_key"}
        return show(cid, mid,
                    "<b>🗑 REVOKE KEY</b>\nSend the full key string.",
                    KB([B("❌ Cancel", cb="adm:keys", style="danger")]))

    if act == "settog":
        set_setting("force_join", "0" if force_join_on() else "1")
        return show(cid, mid, *adm_settings())

    if act == "setlim":
        admin_state[uid] = {"mode": "set_limit"}
        return show(cid, mid, "<b>📊 SET DAILY LIMIT</b>\nSend a number (requests/key/day).",
                    KB([B("❌ Cancel", cb="adm:set", style="danger")]))

    return ack()


def on_admin_input(uid, cid, text):
    st   = admin_state.get(uid) or {}
    mode = st.get("mode")
    back = lambda cb: KB([B("🔙 Back", cb=cb, style="primary")])

    try:
        if mode == "add_channel":
            c = adm_add_channel(text)
            admin_state.pop(uid, None)
            send(cid, f"<b>✅ CHANNEL ADDED</b>\n{E(c['title'] or str(c['chat_id']))}\n"
                      "Users will be required to join on next request.",
                 KB([B("📢 Channels", cb="adm:ch", style="primary")]))

        elif mode == "del_channel":
            if text.strip().isdigit():
                q("DELETE FROM channels WHERE id=?", (int(text.strip()),))
                admin_state.pop(uid, None)
                send(cid, "✅ Channel removed.", KB([B("📢 Channels", cb="adm:ch", style="primary")]))
            else:
                send(cid, "Send the channel number from the list.")

        elif mode == "broadcast":
            try:
                send(cid, "<b>👀 PREVIEW:</b>\n\n" + text + FOOTER)
            except TGError as e:
                send(cid, f"❌ Invalid HTML: {E(e.desc)}"); return
            st["text"] = text
            send(cid, "Send to all users?",
                 KB([B("✅ Send Now", cb="adm:bcgo", style="success"),
                     B("❌ Cancel",   cb="adm:home", style="danger")]))

        elif mode == "find_user":
            if not text.strip().isdigit():
                return send(cid, "❌ Send a numeric user ID.")
            admin_state.pop(uid, None)
            send(cid, *adm_user_card(int(text.strip())))

        elif mode == "gen_key":
            p = text.split()
            if len(p) != 2 or not (p[0].isdigit() and p[1].isdigit()):
                return send(cid, "❌ Format: <code>USER_ID DAYS</code>")
            if not get_user(int(p[0])):
                return send(cid, "❌ User hasn't started the bot.")
            k, exp = grant_key(int(p[0]), int(p[1]), f"Admin {p[1]}d")
            admin_state.pop(uid, None)
            send(cid, f"<b>✅ KEY READY</b>\n<code>{k}</code>\n⏳ {fmt_exp(exp)}", back("adm:keys"))
            try:
                send(int(p[0]), f"<b>🎁 KEY GRANTED!</b>\n<code>{k}</code>\nExpires: {fmt_exp(exp)}" + FOOTER)
            except:
                pass

        elif mode == "revoke_key":
            n = q("UPDATE keys SET revoked=1 WHERE key=?", (text.strip(),))
            admin_state.pop(uid, None)
            send(cid, "✅ Key revoked." if n else "❌ Key not found.", back("adm:keys"))

        elif mode == "set_limit":
            if not text.strip().isdigit() or int(text) < 1:
                return send(cid, "❌ Send a positive number.")
            set_setting("daily_limit", int(text))
            admin_state.pop(uid, None)
            send(cid, f"✅ Daily limit → <b>{int(text)}</b> req/key/day.", back("adm:set"))

    except ValueError as e:
        send(cid, f"❌ {e}")


def _broadcast(admin_cid, text):
    users = q("SELECT id FROM users WHERE banned=0", many=True)
    ok = bad = 0
    for r in users:
        for _ in range(2):
            try:
                send(r["id"], text + FOOTER); ok += 1; break
            except TGError as e:
                if e.retry_after: time.sleep(e.retry_after + 1); continue
                bad += 1; break
        time.sleep(0.05)
    send(admin_cid, f"<b>📣 BROADCAST DONE</b>\n✅ Sent: {ok}\n❌ Failed: {bad}",
         KB([B("🔙 Admin", cb="adm:home", style="primary")]))


# ══════════════════════════════════════════════════════
#  URL / TARGET REGEX
# ══════════════════════════════════════════════════════
TG_TARGET_RE = re.compile(
    r'(?:@[A-Za-z][A-Za-z0-9_]{3,}|(?:-100|-)?\d{5,})'
)


def _extract_target(text):
    """Return the first username or numeric ID in text, or None."""
    m = TG_TARGET_RE.search(text.strip())
    return m.group(0) if m else None


# ══════════════════════════════════════════════════════
#  UPDATE ROUTER
# ══════════════════════════════════════════════════════
def on_message(m):
    chat = m.get("chat") or {}
    if chat.get("type") != "private" or not m.get("from"):
        return
    frm, cid = m["from"], chat["id"]
    uid  = frm["id"]
    text = (m.get("text") or "").strip()
    if not text:
        return

    ref = None
    if text.startswith("/start"):
        p = text.split(maxsplit=1)
        if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit():
            ref = int(p[1][4:])
    row, _new = upsert_user(frm, ref)
    if row["banned"] and uid not in ADMIN_IDS:
        return send(cid, f"🚫 <b>You are banned.</b>\nContact @{E(CONTACT_USERNAME)}." + FOOTER)

    # Commands
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/cancel":
            admin_state.pop(uid, None)
            return send(cid, "✅ Cancelled.")
        if cmd == "/admin":
            if uid not in ADMIN_IDS:
                return
            admin_state.pop(uid, None)
            return send(cid, *adm_home())
        if cmd == "/buy":
            return send(cid, f"<b>💎 BUY PREMIUM</b>\n━━━━━━━━━━━━━━━━━━\n"
                        f"Contact <b>@{E(CONTACT_USERNAME)}</b> for premium plans." + FOOTER,
                        KB([B("💬 Contact to Buy", url=CONTACT_URL, style="success")]))
        if not gate(uid, cid):
            return
        if cmd in ("/start", "/menu", "/help"):
            try_count_referral(uid)
            return send(cid, *screen_menu(uid, frm.get("first_name")))
        if cmd == "/refer":
            return send(cid, *screen_refer(uid))
        if cmd in ("/key", "/mykey"):
            return send(cid, *screen_key(uid))
        if cmd in ("/api", "/docs"):
            return send(cid, *screen_docs(uid))
        return send(cid, *screen_menu(uid, frm.get("first_name")))

    # Admin input
    if uid in ADMIN_IDS and uid in admin_state:
        return on_admin_input(uid, cid, text)

    # TG lookup
    target = _extract_target(text)
    if target:
        if not gate(uid, cid, pending=target):
            return
        try_count_referral(uid)

        # Check key
        key_row = active_key(uid)
        if key_row:
            ok, code, msg = validate_and_count(key_row["key"])
            if not ok:
                return send(cid,
                    f"<b>⚠️ KEY ISSUE</b>\n{E(msg)}\n\nRenew via referrals or buy a plan." + FOOTER,
                    KB([B("🎁 Refer & Earn", cb="refer", style="success"),
                        B("💎 Buy Premium",  url=CONTACT_URL, style="danger")]))
        else:
            return send(cid,
                "<b>🔑 NO API KEY</b>\n━━━━━━━━━━━━━━━━━━\n"
                "You need an API key to use lookups.\n\n"
                "Refer friends to get a free key!" + FOOTER,
                KB([B("🎁 Refer & Earn",  cb="refer", style="success"),
                    B("💎 Buy Premium",   url=CONTACT_URL, style="danger"),
                    B("📖 How to Get Key", cb="mykey", style="primary")]))

        loading = send(cid, f"🔍 <b>Looking up</b> <code>{E(target)}</code>…" + FOOTER)
        mid     = (loading or {}).get("message_id")

        def _do_lookup():
            data   = call_lookup(target)
            result = fmt_lookup_result(data, target)
            show(cid, mid, result, lookup_markup(target))
            q("UPDATE users SET lookup_count=lookup_count+1 WHERE id=?", (uid,))

        threading.Thread(target=_do_lookup, daemon=True).start()
        return

    # Fallback
    if not gate(uid, cid):
        return
    send(cid,
         "<b>🔍 SEND A TARGET</b>\n━━━━━━━━━━━━━━━━━━\n"
         "Send a <b>@username</b> or <b>User/Chat ID</b>:\n\n"
         "Examples:\n<code>@durov</code>\n<code>5765411204</code>\n<code>-1001234567890</code>"
         + FOOTER, menu_markup(uid))


def on_callback(cq):
    uid  = cq["from"]["id"]
    data = cq.get("data") or ""
    msg  = cq.get("message") or {}
    cid  = (msg.get("chat") or {}).get("id")
    mid  = msg.get("message_id")
    done = {"v": False}

    def ack(text=None, alert=False):
        if done["v"]: return
        done["v"] = True
        p = {"callback_query_id": cq["id"]}
        if text: p.update(text=text, show_alert=alert)
        try: tg("answerCallbackQuery", **p)
        except TGError: pass

    if not cid: return ack()
    row, _ = upsert_user(cq["from"])
    if row["banned"] and uid not in ADMIN_IDS:
        return ack("🚫 You are banned.", True)

    if data.startswith("adm:"):
        if uid not in ADMIN_IDS: return ack("Admins only", True)
        on_admin_callback(uid, cid, mid, data, ack)
        return ack()

    if data == "verify":
        _member_cache.clear()
        miss = missing_channels(uid)
        if miss:
            ack("❌ Not all channels joined!", True)
            return join_prompt(cid, mid, miss)
        ack("✅ Verified!")
        try_count_referral(uid)
        target = _pending_target.pop(uid, None)
        if target:
            show(cid, mid, f"🔍 <b>Looking up</b> <code>{E(target)}</code>…" + FOOTER)
            data_result = call_lookup(target)
            result      = fmt_lookup_result(data_result, target)
            return send(cid, result, lookup_markup(target))
        return show(cid, mid, *screen_menu(uid, cq["from"].get("first_name")))

    ack()
    if not data.startswith("claim:") and not gate(uid, cid, mid):
        return

    if data == "noop":   return
    if data == "menu":   return show(cid, mid, *screen_menu(uid, cq["from"].get("first_name")))
    if data == "howto":  return show(cid, mid, *screen_howto(uid))
    if data == "refer":  return show(cid, mid, *screen_refer(uid))
    if data == "mykey":  return show(cid, mid, *screen_key(uid))
    if data == "docs":   return show(cid, mid, *screen_docs(uid))

    if data.startswith("claim:"):
        try:
            idx = int(data.split(":")[1])
            if not 0 <= idx < len(PLANS): return
            res = claim_plan(uid, idx)
        except (ValueError, IndexError): return
        if not res:
            return show(cid, mid,
                f"<b>❌ NOT ENOUGH POINTS</b>\nYou need {PLANS[idx][0]} referral points." + FOOTER,
                KB([B("🎁 Refer & Earn", cb="refer", style="success")]))
        key, exp = res
        return show(cid, mid, *key_card(key, exp, PLANS[idx][2]))


def _safe(fn, obj):
    try: fn(obj)
    except Exception as e:
        print(f"[Bot] handler error: {type(e).__name__}: {e}", flush=True)


def _poll_loop():
    pool   = ThreadPoolExecutor(max_workers=24, thread_name_prefix="tgbot")
    offset = None
    try:
        tg("deleteWebhook", drop_pending_updates=False)
    except TGError: pass
    print(f"[Bot] @{BOT_USERNAME} polling…", flush=True)
    while True:
        try:
            params = dict(timeout=50, allowed_updates=["message", "callback_query"])
            if offset is not None: params["offset"] = offset
            for u in tg("getUpdates", _t=70, **params):
                offset = u["update_id"] + 1
                if "message" in u:
                    pool.submit(_safe, on_message, u["message"])
                elif "callback_query" in u:
                    pool.submit(_safe, on_callback, u["callback_query"])
        except TGError as e:
            print(f"[Bot] poll error: {e.desc}", flush=True)
            time.sleep(max(3, e.retry_after))
        except Exception as e:
            print(f"[Bot] poll crash: {e}", flush=True)
            time.sleep(5)


# ══════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════
def start_bot(port):
    global BOT_TOKEN, BOT_ID, BOT_USERNAME, BASE_URL, LOCAL_PORT, ADMIN_IDS
    LOCAL_PORT = port
    BASE_URL   = os.environ.get("BASE_URL", f"http://localhost:{port}").strip().rstrip("/")
    ADMIN_IDS  = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS", ""))}
    BOT_TOKEN  = os.environ.get("BOT_TOKEN", "").strip()
    if not BOT_TOKEN:
        print("⚠️  BOT_TOKEN not set — bot disabled (API stays up).", flush=True)
        return False
    try:
        me = tg("getMe")
    except TGError as e:
        print(f"⚠️  Bot token rejected: {e.desc}", flush=True)
        return False
    BOT_ID, BOT_USERNAME = me["id"], me.get("username", "")
    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS empty — no one can use /admin in bot.", flush=True)
    threading.Thread(target=_poll_loop, daemon=True, name="tg-poll").start()
    return True


# ══════════════════════════════════════════════════════
#  API GATE (Flask middleware)
# ══════════════════════════════════════════════════════
def _require_key():
    v = os.environ.get("REQUIRE_API_KEY")
    if v is None:
        return bool(os.environ.get("BOT_TOKEN", "").strip())
    return v.lower() in ("1", "true", "yes", "on")


def install_api_gate(app, developer, is_admin=None):
    from flask import request, jsonify

    @app.before_request
    def _gate():
        if not _require_key():
            return None
        if request.path.rstrip("/") not in ("/lookup",):
            return None
        if request.headers.get("X-Internal-Token") == INTERNAL_TOKEN:
            return None
        if is_admin and is_admin():
            return None
        key = (request.headers.get("X-API-Key")
               or request.args.get("key") or request.args.get("api_key") or "")
        if not key and request.is_json:
            body = request.get_json(silent=True) or {}
            key  = body.get("key") or body.get("api_key") or ""
        ok, code, msg = validate_and_count(str(key).strip())
        if ok:
            return None
        return jsonify({
            "status":  False, "developer": developer, "message": msg,
            "get_key": f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else None,
            "buy":     CONTACT_URL,
        }), code
