"""Multi-tenant Business AI Agent API.   Run: uvicorn app:app --reload"""
import logging
import secrets
import threading
import time
from datetime import datetime

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

import agent
import catalog
import db
import ratelimit
import settings
import whatsapp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

if settings.ENV == "production":  # fail closed on weak/missing secrets
    _problems = settings.production_problems()
    if _problems:
        raise RuntimeError("Refusing to start in production: " + "; ".join(_problems))
db.init()


def _purge_loop():
    time.sleep(60)
    while True:
        try:
            log.info("retention purge: %s", db.purge_old())
        except Exception:
            log.exception("retention purge failed")
        time.sleep(86400)


threading.Thread(target=_purge_loop, daemon=True, name="purge").start()

_prod = settings.ENV == "production"
app = FastAPI(title="Business AI Agent API", docs_url=None if _prod else "/docs",
              redoc_url=None, openapi_url=None if _prod else "/openapi.json")
# CORS only controls which sites a *browser* may read responses for. There are no cookies here; the real gate is
# the tenant key plus the per-tenant allowed_origins check in tenant_public(). Static CORS can't express per-tenant origins.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(whatsapp.router)

if not _prod:
    @app.get("/", include_in_schema=False)
    def local_home():
        return RedirectResponse(url="/docs")


# -------------------------------------------------------------- auth deps
def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest((a or "").encode(), (b or "").encode())


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _guard(request: Request, kind: str, limit: int) -> None:
    if ratelimit.blocked(f"authfail:{kind}:{_ip(request)}", limit, 300):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")


def _fail(request: Request, kind: str, msg: str = "Invalid credentials"):
    ratelimit.record(f"authfail:{kind}:{_ip(request)}")
    raise HTTPException(401, msg)


def super_admin(request: Request, x_super_key: str = Header(default="")):
    _guard(request, "super", 10)
    if not settings.SUPER_ADMIN_KEY or not _eq(x_super_key, settings.SUPER_ADMIN_KEY):
        _fail(request, "super", "Invalid super admin key")


def tenant_public(tenant_id: str, request: Request, x_tenant_key: str = Header(default="")):
    _guard(request, "pub", 40)
    t = db.get_tenant(tenant_id)
    if not t or not _eq(x_tenant_key, t["public_key"]):
        _fail(request, "pub", "Invalid tenant or key")
    allowed, origin = t["config"].get("allowed_origins") or [], request.headers.get("origin")
    if allowed and origin and origin not in allowed:
        raise HTTPException(403, "Origin not allowed")
    return t


def tenant_admin(tenant_id: str, request: Request, x_admin_key: str = Header(default="")):
    _guard(request, "admin", 10)
    t = db.get_tenant(tenant_id)
    if not t or not db.check_admin(t, x_admin_key):
        _fail(request, "admin", "Invalid tenant or admin key")
    return t


# ------------------------------------------------------------------ public
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/widget")
def widget():
    return FileResponse("static/widget.html")


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.:@+-]+$")
    message: str = Field(min_length=1, max_length=1500)


@app.post("/api/v1/{tenant_id}/chat")
def chat(tenant_id: str, body: ChatIn, request: Request, tenant: dict = Depends(tenant_public)):
    ratelimit.check(f"ip:{tenant_id}:{_ip(request)}", 40)
    ratelimit.check(f"s:{tenant_id}:{body.session_id}", 15)
    if agent.over_daily_limit(tenant):
        raise HTTPException(429, agent.LIMIT_MSG)
    # The server owns the session namespace: a web caller can NEVER address a WhatsApp ("wa:...") session.
    return {"response": agent.run_agent(tenant, f"web:{body.session_id}", body.message)}


@app.delete("/api/v1/{tenant_id}/chat/{session_id}")
def reset_session(tenant_id: str, session_id: str, tenant: dict = Depends(tenant_public)):
    db.clear_session(tenant_id, f"web:{session_id}")
    return {"status": "cleared"}


# ------------------------------------------------------- super admin (you)
class TenantIn(BaseModel):
    tenant_id: str
    business_name: str = Field(min_length=1, max_length=120)
    config: dict = {}


@app.post("/admin/tenants", dependencies=[Depends(super_admin)])
def create_tenant(body: TenantIn):
    try:
        pk, ak = db.create_tenant(body.tenant_id, body.business_name, body.config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tenant_id": body.tenant_id, "public_key": pk, "admin_key": ak,
            "note": "Store admin_key now; it cannot be shown again.",
            "widget_path": f"/widget?t={body.tenant_id}&k={pk}"}


@app.get("/admin/tenants", dependencies=[Depends(super_admin)])
def list_tenants():
    return db.q("SELECT id,name,created_at FROM tenants")


@app.post("/admin/tenants/{tenant_id}/reset-keys", dependencies=[Depends(super_admin)])
def reset_keys(tenant_id: str):
    """For a lost or leaked key: issues a new public AND admin key; the old ones stop working at once."""
    if not db.get_tenant(tenant_id):
        raise HTTPException(404, "Unknown tenant")
    return {"tenant_id": tenant_id, **db.rotate_keys(tenant_id)}


# --------------------------------------------------- per-client admin (client)
@app.post("/api/v1/{tenant_id}/rotate-admin-key")
def rotate_admin_key(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    return db.rotate_keys(tenant_id, public=False, admin=True)


@app.put("/api/v1/{tenant_id}/config")
def update_config(tenant_id: str, patch: dict, tenant: dict = Depends(tenant_admin)):
    try:
        cfg = db.update_config(tenant_id, patch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    wa = cfg.get("whatsapp", {})  # never echo secrets
    cfg["whatsapp"] = {"phone_number_id": wa.get("phone_number_id", ""), "token_set": bool(wa.get("token_enc"))}
    return cfg


@app.post("/api/v1/{tenant_id}/catalog")
def upload_catalog(tenant_id: str, file: UploadFile = File(...), tenant: dict = Depends(tenant_admin)):
    raw = file.file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "File too large (max 2 MB)")
    try:
        count = catalog.index_csv(tenant_id, raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, str(e))
    except Exception:
        log.exception("Catalog indexing failed")
        raise HTTPException(502, "Indexing failed. Your previous catalog is unchanged; please try again.")
    return {"status": "indexed", "products": count}


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(ge=0, le=1e9)
    stock_count: int = Field(ge=0, le=1_000_000)
    category: str = Field(default="", max_length=100)
    fields: dict[str, str] = Field(default_factory=dict)  # extra columns: description, dimensions, year...


class ProductPatch(BaseModel):
    price: float | None = Field(default=None, ge=0, le=1e9)
    stock_count: int | None = Field(default=None, ge=0, le=1_000_000)


@app.put("/api/v1/{tenant_id}/products/{product_id}")
def put_product(tenant_id: str, product_id: str, body: ProductIn, tenant: dict = Depends(tenant_admin)):
    raw = {**body.fields, "id": product_id, "name": body.name, "price": str(body.price),
           "stock_count": str(body.stock_count), "category": body.category}
    try:
        row = catalog.upsert_product(tenant_id, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        log.exception("Product upsert failed")
        raise HTTPException(502, "Saving failed; nothing was changed.")
    return {"status": "saved", "id": row["id"]}


@app.patch("/api/v1/{tenant_id}/products/{product_id}")
def patch_product(tenant_id: str, product_id: str, body: ProductPatch, tenant: dict = Depends(tenant_admin)):
    if body.price is None and body.stock_count is None:
        raise HTTPException(400, "Send price and/or stock_count")
    try:
        catalog.patch_product(tenant_id, product_id, body.price, body.stock_count)
    except KeyError:
        raise HTTPException(404, "Unknown product")
    return {"status": "updated"}


@app.delete("/api/v1/{tenant_id}/products/{product_id}")
def delete_product(tenant_id: str, product_id: str, tenant: dict = Depends(tenant_admin)):
    try:
        catalog.delete_product(tenant_id, product_id)
    except KeyError:
        raise HTTPException(404, "Unknown product")
    return {"status": "deleted"}


@app.get("/api/v1/{tenant_id}/leads")
def leads(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    return db.q("SELECT * FROM leads WHERE tenant_id=? ORDER BY id DESC LIMIT 500", (tenant_id,))


@app.get("/api/v1/{tenant_id}/bookings")
def bookings(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    return db.q("SELECT * FROM bookings WHERE tenant_id=? ORDER BY date DESC, time DESC LIMIT 500", (tenant_id,))


@app.get("/api/v1/{tenant_id}/handoffs")
def handoffs(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    return db.q("SELECT * FROM handoffs WHERE tenant_id=? ORDER BY id DESC LIMIT 500", (tenant_id,))


@app.get("/api/v1/{tenant_id}/conversations/{session_id}")
def conversation(tenant_id: str, session_id: str, tenant: dict = Depends(tenant_admin)):
    """Admin view. Session ids are stored with their channel prefix: 'web:<id>' or 'wa:<phone>'."""
    return db.q("SELECT role,content,created_at FROM messages WHERE tenant_id=? AND session_id=? ORDER BY id",
                (tenant_id, session_id))


# --------------------------------------------------------- privacy / erasure
@app.delete("/api/v1/{tenant_id}/conversations/{session_id}")
def delete_conversation(tenant_id: str, session_id: str, tenant: dict = Depends(tenant_admin)):
    return {"deleted_messages": db.delete_conversation(tenant_id, session_id)}


@app.delete("/api/v1/{tenant_id}/customers")
def erase_customer(tenant_id: str, phone: str = "", email: str = "", tenant: dict = Depends(tenant_admin)):
    """Right-to-erasure: removes the customer's leads, bookings, handoffs and WhatsApp conversation."""
    try:
        return db.erase_person(tenant_id, phone, email)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------- offers & results (client)
class PromoIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=300)
    discount_pct: float = Field(default=0, ge=0, le=90)
    discount_amount: float = Field(default=0, ge=0)
    category: str = Field(default="", max_length=60)   # blank = whole catalog
    product_id: str = Field(default="", max_length=60)  # set to target one product
    code: str = Field(default="", max_length=30)
    starts_on: str = ""                                  # YYYY-MM-DD, blank = already started
    ends_on: str = ""                                    # YYYY-MM-DD, blank = no end

    @field_validator("starts_on", "ends_on")
    @classmethod
    def _date(cls, v: str) -> str:
        if v:
            datetime.strptime(v, "%Y-%m-%d")
        return v


@app.post("/api/v1/{tenant_id}/promotions")
def add_promotion(tenant_id: str, body: PromoIn, tenant: dict = Depends(tenant_admin)):
    if body.discount_pct and body.discount_amount:
        raise HTTPException(400, "Use either discount_pct or discount_amount, not both")
    return {"id": db.add_promotion(tenant_id, body.model_dump())}


@app.get("/api/v1/{tenant_id}/promotions")
def list_promotions(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    return db.list_promotions(tenant_id)


@app.delete("/api/v1/{tenant_id}/promotions/{promo_id}")
def delete_promotion(tenant_id: str, promo_id: int, tenant: dict = Depends(tenant_admin)):
    db.delete_promotion(tenant_id, promo_id)
    return {"status": "deleted"}


@app.get("/api/v1/{tenant_id}/stats")
def stats(tenant_id: str, tenant: dict = Depends(tenant_admin)):
    cfg = tenant["config"]
    return {**db.stats(tenant_id), "messages_today": db.messages_today(tenant_id, cfg["timezone"]),
            "daily_message_limit": cfg["daily_message_limit"]}
