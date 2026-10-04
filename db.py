"""SQLite storage (free, zero-setup). One file: DATA_DIR/agent.db"""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import secrets_box
import settings

_local = threading.local()
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}[a-z0-9]$")
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_CONFIG = {
    "persona": "You are a warm, professional sales consultant.",
    "timezone": "Africa/Nairobi",
    "currency": "KES",
    "slot_times": ["10:00", "13:30", "15:00"],
    "open_days": [0, 1, 2, 3, 4, 5],  # Mon=0 ... Sun=6
    "appointment_minutes": 60,
    "owner_email": "",
    "allowed_origins": [],       # e.g. ["https://shop.co.ke"]; empty = any website may embed the widget
    "whatsapp": {},              # write: {"phone_number_id": "...", "token": "..."}; token is stored encrypted
    "business_facts": "",        # real selling points: delivery, warranty, payment options, location...
    "daily_message_limit": 300,  # customer messages per day for this client (protects your LLM quota)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL,
  public_key TEXT NOT NULL, admin_key_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  catalog_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, category TEXT,
  price REAL, stock INTEGER, data TEXT NOT NULL, PRIMARY KEY(tenant_id, id));
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT, email TEXT,
  phone TEXT, date TEXT NOT NULL, time TEXT NOT NULL, product TEXT, created_at TEXT,
  UNIQUE(tenant_id, date, time));
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT, email TEXT,
  phone TEXT, interest TEXT, source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS handoffs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, session_id TEXT,
  reason TEXT, contact TEXT, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_msg ON messages(tenant_id, session_id, id);
CREATE TABLE IF NOT EXISTS promotions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT DEFAULT '', discount_pct REAL DEFAULT 0, discount_amount REAL DEFAULT 0,
  category TEXT DEFAULT '', product_id TEXT DEFAULT '', code TEXT DEFAULT '',
  starts_on TEXT DEFAULT '', ends_on TEXT DEFAULT '', active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS tool_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, session_id TEXT, tool TEXT,
  args TEXT, result TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS wa_seen(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(settings.DATA_DIR / "agent.db", timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)
    cols = {r["name"] for r in q("PRAGMA table_info(tenants)")}
    if "catalog_version" not in cols:  # upgrade from the earlier schema
        x("ALTER TABLE tenants ADD COLUMN catalog_version INTEGER NOT NULL DEFAULT 0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def q(sql, params=(), one=False):
    rows = conn().execute(sql, params).fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def x(sql, params=()) -> int:
    c = conn()
    cur = c.execute(sql, params)
    c.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ tenants
def validate_config(patch: dict) -> dict:
    out = {}
    for k, v in patch.items():
        if k not in DEFAULT_CONFIG:
            raise ValueError(f"Unknown setting: {k}")
        if k == "timezone":
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError("Invalid timezone, e.g. Africa/Nairobi")
        elif k == "slot_times":
            if not isinstance(v, list) or not v or not all(isinstance(t, str) and TIME.match(t) for t in v):
                raise ValueError("slot_times must be a non-empty list of HH:MM (24h)")
            v = sorted(set(v))
        elif k == "open_days":
            if not isinstance(v, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in v):
                raise ValueError("open_days must be a list of 0-6 (Mon=0)")
        elif k in ("appointment_minutes", "daily_message_limit"):
            lo, hi = (15, 480) if k == "appointment_minutes" else (1, 100_000)
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(f"{k} must be an integer {lo}-{hi}")
        elif k == "allowed_origins":
            if not isinstance(v, list) or not all(isinstance(o, str) for o in v):
                raise ValueError("allowed_origins must be a list of strings")
        elif k == "whatsapp":
            if not isinstance(v, dict) or set(v) - {"phone_number_id", "token"} or not all(isinstance(i, str) for i in v.values()):
                raise ValueError('whatsapp must be {"phone_number_id": "...", "token": "..."}')
        else:  # persona, currency, owner_email, business_facts
            if not isinstance(v, str) or len(v) > 1500:
                raise ValueError(f"{k} must be a string (max 1500 chars)")
        out[k] = v
    return out


def _merge_whatsapp(old: dict, new: dict) -> dict:
    """Keeps the phone_number_id readable but stores the access token encrypted (token_enc)."""
    out = dict(old)
    if "phone_number_id" in new:
        out["phone_number_id"] = new["phone_number_id"].strip()
    if new.get("token"):
        out["token_enc"] = secrets_box.encrypt(new["token"].strip())
    return out


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_tenant(tenant_id: str, name: str, config: dict | None = None) -> tuple[str, str]:
    if not SLUG.match(tenant_id):
        raise ValueError("tenant_id must be 3-42 chars: lowercase letters, digits, - or _")
    if q("SELECT 1 FROM tenants WHERE id=?", (tenant_id,), one=True):
        raise ValueError("Tenant already exists")
    cfg = validate_config(config or {})
    if "whatsapp" in cfg:
        cfg["whatsapp"] = _merge_whatsapp({}, cfg["whatsapp"])
    public_key, admin_key = "pk_" + secrets.token_urlsafe(18), "ak_" + secrets.token_urlsafe(24)
    x("INSERT INTO tenants(id,name,config,public_key,admin_key_hash,created_at) VALUES(?,?,?,?,?,?)",
      (tenant_id, name, json.dumps(cfg), public_key, _hash(admin_key), now()))
    return public_key, admin_key


def get_tenant(tenant_id: str) -> dict | None:
    row = q("SELECT * FROM tenants WHERE id=?", (tenant_id,), one=True)
    if not row:
        return None
    row["config"] = {**DEFAULT_CONFIG, **json.loads(row["config"])}
    return row


def check_admin(tenant: dict, key: str) -> bool:
    return secrets.compare_digest(_hash(key or ""), tenant["admin_key_hash"])


def rotate_keys(tenant_id: str, public: bool = True, admin: bool = True) -> dict:
    """Issue new keys (old ones stop working immediately). Returns the new plaintext keys once."""
    out = {}
    if public:
        out["public_key"] = "pk_" + secrets.token_urlsafe(18)
        x("UPDATE tenants SET public_key=? WHERE id=?", (out["public_key"], tenant_id))
    if admin:
        out["admin_key"] = "ak_" + secrets.token_urlsafe(24)
        x("UPDATE tenants SET admin_key_hash=? WHERE id=?", (_hash(out["admin_key"]), tenant_id))
    return out


def update_config(tenant_id: str, patch: dict) -> dict:
    stored = json.loads(q("SELECT config FROM tenants WHERE id=?", (tenant_id,), one=True)["config"])
    patch = validate_config(patch)
    if "whatsapp" in patch:
        patch["whatsapp"] = _merge_whatsapp(stored.get("whatsapp", {}), patch["whatsapp"])
    stored.update(patch)
    x("UPDATE tenants SET config=? WHERE id=?", (json.dumps(stored), tenant_id))
    return get_tenant(tenant_id)["config"]


def find_tenant_by_whatsapp(phone_number_id: str) -> dict | None:
    for r in q("SELECT id FROM tenants"):
        t = get_tenant(r["id"])
        if str(t["config"].get("whatsapp", {}).get("phone_number_id", "")) == str(phone_number_id):
            return t
    return None


# ----------------------------------------------------------------- products
def catalog_version(tenant_id: str) -> int:
    r = q("SELECT catalog_version v FROM tenants WHERE id=?", (tenant_id,), one=True)
    return r["v"] if r else 0


def set_catalog_version(tenant_id: str, version: int) -> None:
    x("UPDATE tenants SET catalog_version=? WHERE id=?", (version, tenant_id))


def replace_products(tenant_id: str, rows: list[dict], version: int | None = None) -> None:
    """Swap the whole catalog AND the active index version in ONE transaction (the atomic switch)."""
    c = conn()
    with c:
        c.execute("DELETE FROM products WHERE tenant_id=?", (tenant_id,))
        c.executemany(
            "INSERT INTO products VALUES(?,?,?,?,?,?,?)",
            [(tenant_id, r["id"], r["name"], r["category"], r["price"], r["stock"], json.dumps(r["data"]))
             for r in rows])
        if version is not None:
            c.execute("UPDATE tenants SET catalog_version=? WHERE id=?", (version, tenant_id))


def upsert_product(tenant_id: str, r: dict) -> None:
    x("INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?,?)",
      (tenant_id, r["id"], r["name"], r["category"], r["price"], r["stock"], json.dumps(r["data"])))


def update_product_fields(tenant_id: str, pid: str, price: float | None, stock: int | None) -> bool:
    row = q("SELECT * FROM products WHERE tenant_id=? AND id=?", (tenant_id, pid), one=True)
    if not row:
        return False
    data = json.loads(row["data"])
    if price is not None:
        row["price"], data["price"] = price, str(price)
    if stock is not None:
        row["stock"], data["stock_count"] = stock, str(stock)
    x("UPDATE products SET price=?, stock=?, data=? WHERE tenant_id=? AND id=?",
      (row["price"], row["stock"], json.dumps(data), tenant_id, pid))
    return True


def delete_product(tenant_id: str, pid: str) -> bool:
    c = conn()
    with c:
        return c.execute("DELETE FROM products WHERE tenant_id=? AND id=?", (tenant_id, pid)).rowcount > 0


def get_products(tenant_id: str, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    out = q(f"SELECT * FROM products WHERE tenant_id=? AND id IN ({marks})", (tenant_id, *ids))
    for p in out:
        p["data"] = json.loads(p["data"])
    return out


def filter_products(tenant_id, category="", min_price=None, max_price=None, in_stock_only=False) -> list[dict]:
    """Exact structured filtering (category/name match, price range, stock). Sorted by price."""
    sql, params = "SELECT * FROM products WHERE tenant_id=?", [tenant_id]
    if category:
        like = f"%{category.lower()}%"
        sql += " AND (LOWER(category) LIKE ? OR LOWER(name) LIKE ?)"
        params += [like, like]
    if min_price is not None:
        sql += " AND price>=?"
        params.append(min_price)
    if max_price is not None:
        sql += " AND price<=?"
        params.append(max_price)
    if in_stock_only:
        sql += " AND stock>0"
    out = q(sql + " ORDER BY price", tuple(params))
    for p in out:
        p["data"] = json.loads(p["data"])
    return out


def category_stats(tenant_id) -> list[dict]:
    return q("SELECT COALESCE(NULLIF(category,''),'Other') AS category, COUNT(*) AS n, MIN(price) AS lo, "
             "MAX(price) AS hi FROM products WHERE tenant_id=? GROUP BY 1 ORDER BY n DESC", (tenant_id,))


# ------------------------------------------------------------ phone helpers
def norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def same_phone(a: str, b: str) -> bool:
    """0712345678 == +254 712 345 678 (compares the last 9 digits)."""
    a, b = norm_phone(a), norm_phone(b)
    if not a or not b:
        return False
    return a == b if min(len(a), len(b)) < 7 else a[-9:] == b[-9:]


# ------------------------------------------------- bookings / leads / handoffs
def booked_times(tenant_id: str, day: str) -> set[str]:
    return {r["time"] for r in q("SELECT time FROM bookings WHERE tenant_id=? AND date=?", (tenant_id, day))}


def insert_booking(tenant_id, name, email, phone, day, time_, product) -> int:
    """Raises sqlite3.IntegrityError if the slot is already taken (UNIQUE constraint)."""
    return x("INSERT INTO bookings(tenant_id,name,email,phone,date,time,product,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant_id, name, email, norm_phone(phone), day, time_, product, now()))


def add_lead(tenant_id, name, email, phone, interest, source) -> int:
    """Insert, or merge into an existing lead with the same email or phone (no duplicates)."""
    email, phone, name, interest = (email or "").strip(), norm_phone(phone), (name or "").strip(), (interest or "").strip()
    cands = []
    if email:
        cands += q("SELECT * FROM leads WHERE tenant_id=? AND LOWER(email)=LOWER(?)", (tenant_id, email))
    if phone:
        cands += [r for r in q("SELECT * FROM leads WHERE tenant_id=? AND phone!=''", (tenant_id,))
                  if same_phone(r["phone"], phone)]
    if cands:
        lead = cands[0]
        new_interest = lead["interest"] or ""
        if interest and interest.lower() not in new_interest.lower():
            new_interest = f"{new_interest}; {interest}".strip("; ")
        x("UPDATE leads SET name=?, email=?, phone=?, interest=? WHERE id=?",
          (lead["name"] or name, lead["email"] or email, lead["phone"] or phone, new_interest, lead["id"]))
        return lead["id"]
    return x("INSERT INTO leads(tenant_id,name,email,phone,interest,source,created_at) VALUES(?,?,?,?,?,?,?)",
            (tenant_id, name, email, phone, interest, source, now()))


def add_handoff(tenant_id, session_id, reason, contact) -> int:
    return x("INSERT INTO handoffs(tenant_id,session_id,reason,contact,created_at) VALUES(?,?,?,?,?)",
            (tenant_id, session_id, reason, contact, now()))


# ---------------------------------------------------------------- messages
def add_message(tenant_id, session_id, role, content) -> None:
    x("INSERT INTO messages(tenant_id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
      (tenant_id, session_id, role, content, now()))


def recent_messages(tenant_id, session_id, n=16) -> list[dict]:
    rows = q("SELECT role,content FROM messages WHERE tenant_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
             (tenant_id, session_id, n))
    return rows[::-1]


def clear_session(tenant_id, session_id) -> None:
    delete_conversation(tenant_id, session_id)


def delete_conversation(tenant_id, session_id) -> int:
    c = conn()
    with c:
        n = c.execute("DELETE FROM messages WHERE tenant_id=? AND session_id=?", (tenant_id, session_id)).rowcount
        c.execute("DELETE FROM tool_logs WHERE tenant_id=? AND session_id=?", (tenant_id, session_id))
    return n


def log_tool(tenant_id, session_id, tool, args, result) -> None:
    x("INSERT INTO tool_logs(tenant_id,session_id,tool,args,result,created_at) VALUES(?,?,?,?,?,?)",
      (tenant_id, session_id, tool, args[:1000], result[:2000], now()))


def messages_today(tenant_id: str, tz_name: str) -> int:
    start = datetime.now(ZoneInfo(tz_name)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    return q("SELECT COUNT(*) n FROM messages WHERE tenant_id=? AND role='user' AND created_at>=?",
             (tenant_id, start_utc), one=True)["n"]


def seen_before(message_id: str) -> bool:
    """Persistent webhook de-duplication (survives restarts). True if this id was already processed."""
    c = conn()
    cur = c.execute("INSERT OR IGNORE INTO wa_seen(id,created_at) VALUES(?,?)", (message_id, now()))
    c.commit()
    return cur.rowcount == 0


# --------------------------------------------- privacy: erasure and retention
def erase_person(tenant_id: str, phone: str = "", email: str = "") -> dict:
    """Delete everything we hold about one customer (by phone and/or email)."""
    phone_n, email_l = norm_phone(phone), (email or "").strip().lower()
    if not phone_n and not email_l:
        raise ValueError("Provide a phone number or an email")
    if phone_n and len(phone_n) < 7:
        raise ValueError("Phone number is too short")

    def hit(r: dict, p_field: str, e_field: str | None) -> bool:
        by_email = bool(email_l and e_field and (r.get(e_field) or "").lower() == email_l)
        by_phone = bool(phone_n and same_phone(r.get(p_field, ""), phone_n))
        return by_email or by_phone

    c, counts = conn(), {}
    with c:
        for table in ("leads", "bookings"):
            ids = [r["id"] for r in q(f"SELECT id,email,phone FROM {table} WHERE tenant_id=?", (tenant_id,))
                   if hit(r, "phone", "email")]
            c.executemany(f"DELETE FROM {table} WHERE id=?", [(i,) for i in ids])
            counts[table] = len(ids)
        ids = [r["id"] for r in q("SELECT id,contact FROM handoffs WHERE tenant_id=?", (tenant_id,))
               if hit({"phone": r["contact"], "email": r["contact"]}, "phone", "email")]
        c.executemany("DELETE FROM handoffs WHERE id=?", [(i,) for i in ids])
        counts["handoffs"] = len(ids)
        sessions = [r["session_id"] for r in q(
            "SELECT DISTINCT session_id FROM messages WHERE tenant_id=? AND session_id LIKE 'wa:%'", (tenant_id,))
            if phone_n and same_phone(r["session_id"][3:], phone_n)]
        for s in sessions:
            c.execute("DELETE FROM messages WHERE tenant_id=? AND session_id=?", (tenant_id, s))
            c.execute("DELETE FROM tool_logs WHERE tenant_id=? AND session_id=?", (tenant_id, s))
        counts["whatsapp_conversations"] = len(sessions)
    return counts


def purge_old() -> dict:
    """Retention: drop old chat history, tool logs and webhook ids. Leads/bookings are business records and stay."""
    def cut(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    c = conn()
    with c:
        m = c.execute("DELETE FROM messages WHERE created_at<?", (cut(settings.RETENTION_MESSAGE_DAYS),)).rowcount
        t = c.execute("DELETE FROM tool_logs WHERE created_at<?", (cut(settings.RETENTION_LOG_DAYS),)).rowcount
        c.execute("DELETE FROM wa_seen WHERE created_at<?", (cut(3),))
    return {"messages": m, "tool_logs": t}


# -------------------------------------------------------------- promotions
def add_promotion(tenant_id: str, p: dict) -> int:
    return x("INSERT INTO promotions(tenant_id,title,description,discount_pct,discount_amount,category,"
             "product_id,code,starts_on,ends_on,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
             (tenant_id, p["title"], p["description"], p["discount_pct"], p["discount_amount"],
              p["category"], p["product_id"], p["code"], p["starts_on"], p["ends_on"], now()))


def list_promotions(tenant_id: str) -> list[dict]:
    return q("SELECT * FROM promotions WHERE tenant_id=? ORDER BY id DESC", (tenant_id,))


def active_promotions(tenant_id: str, today: str) -> list[dict]:
    """Only offers that are switched on and inside their date window (dates are YYYY-MM-DD)."""
    return q("SELECT * FROM promotions WHERE tenant_id=? AND active=1 AND (starts_on='' OR starts_on<=?) "
             "AND (ends_on='' OR ends_on>=?)", (tenant_id, today, today))


def delete_promotion(tenant_id: str, promo_id: int) -> None:
    x("DELETE FROM promotions WHERE tenant_id=? AND id=?", (tenant_id, promo_id))


def stats(tenant_id: str) -> dict:
    def n(sql):
        return q(sql, (tenant_id,), one=True)["n"]
    convs = n("SELECT COUNT(DISTINCT session_id) n FROM messages WHERE tenant_id=?")
    bookings = n("SELECT COUNT(*) n FROM bookings WHERE tenant_id=?")
    return {
        "conversations": convs,
        "messages": n("SELECT COUNT(*) n FROM messages WHERE tenant_id=?"),
        "leads": n("SELECT COUNT(*) n FROM leads WHERE tenant_id=?"),
        "bookings": bookings,
        "handoffs_open": n("SELECT COUNT(*) n FROM handoffs WHERE tenant_id=? AND status='open'"),
        "offers_shown": n("SELECT COUNT(*) n FROM tool_logs WHERE tenant_id=? AND result LIKE '%Offer:%'"),
        "conversation_to_booking_pct": round(100 * bookings / convs, 1) if convs else 0,
    }
