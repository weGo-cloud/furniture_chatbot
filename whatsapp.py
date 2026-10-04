"""WhatsApp Cloud API webhook. Each tenant stores its phone_number_id and an ENCRYPTED token in config.whatsapp."""
import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

import agent
import db
import ratelimit
import secrets_box
import settings

log = logging.getLogger("whatsapp")
router = APIRouter()


@router.get("/webhooks/whatsapp")
def verify(request: Request):
    p = request.query_params
    token = settings.WHATSAPP_VERIFY_TOKEN
    if p.get("hub.mode") == "subscribe" and token and hmac.compare_digest(p.get("hub.verify_token", "").encode(), token.encode()):
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "Verification failed")


def _token(tenant: dict) -> str:
    enc = tenant["config"].get("whatsapp", {}).get("token_enc")
    return secrets_box.decrypt(enc) if enc else ""


def _send(tenant: dict, to: str, text: str) -> None:
    wa = tenant["config"].get("whatsapp", {})
    url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{wa.get('phone_number_id')}/messages"
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {_token(tenant)}"}, timeout=20,
                       json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
        if r.status_code >= 300:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
    except Exception:
        log.exception("WhatsApp send error")


def _handle(tenant: dict, sender: str, text: str) -> None:
    try:
        ratelimit.check(f"wa:{tenant['id']}:{sender}", 15)
    except HTTPException:
        return
    if agent.over_daily_limit(tenant):
        _send(tenant, sender, agent.LIMIT_MSG)
        return
    _send(tenant, sender, agent.run_agent(tenant, f"wa:{sender}", text, default_phone=sender))


@router.post("/webhooks/whatsapp")
async def receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    secret = settings.WHATSAPP_APP_SECRET
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", "").encode(), expected.encode()):
            raise HTTPException(403, "Bad signature")
    elif settings.ENV == "production":  # fail closed: never accept unsigned webhooks in production
        raise HTTPException(503, "WhatsApp webhook disabled: WHATSAPP_APP_SECRET is not set")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Bad JSON")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            pnid = value.get("metadata", {}).get("phone_number_id")
            tenant = db.find_tenant_by_whatsapp(pnid) if pnid else None
            if not tenant:
                continue
            for m in value.get("messages", []):
                mid = m.get("id")
                if m.get("type") != "text" or not mid or db.seen_before(mid):
                    continue
                bg.add_task(_handle, tenant, m["from"], m["text"]["body"][:1500])
    return {"status": "ok"}  # always 200 fast so Meta doesn't retry
