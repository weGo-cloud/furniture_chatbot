# Production deploy (any small VPS, ~1 GB RAM minimum)

Needs: a VPS with Docker (Oracle Always Free, Hetzner, DigitalOcean...), a domain/subdomain pointing at the server's IP.

```
git clone <your repo> && cd furniture-agent
cp .env.example .env     # set LLM_API_KEY, SUPER_ADMIN_KEY (long random), DOMAIN, SMTP_* if wanted
docker compose up -d --build
curl https://$DOMAIN/health
```
Caddy fetches the HTTPS certificate automatically. The app port is not exposed publicly; only Caddy (80/443) is.

## Local development
Set `LLM_API_KEY` in `.env`, then run the app directly (without the production Caddy proxy):
```
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build app
```
Open `http://localhost:8000/docs`. The local override enables development mode and binds port 8000 to loopback only; do not use it for production.

## First client
```
H="https://$DOMAIN"
curl -X POST $H/admin/tenants -H "X-Super-Key: $SUPER_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme","business_name":"Acme Furniture","config":{
        "owner_email":"owner@acme.co.ke","allowed_origins":["https://acme.co.ke"],
        "business_facts":"Free delivery within Nairobi. 12-month warranty. M-Pesa and bank transfer accepted. Showroom on Ngong Road."}}'
curl -X POST $H/api/v1/acme/catalog -H "X-Admin-Key: ak_..." -F file=@catalog.csv
curl -X POST $H/api/v1/acme/promotions -H "X-Admin-Key: ak_..." -H "Content-Type: application/json" \
  -d '{"title":"October Sale","discount_pct":10,"category":"Living Room","ends_on":"2026-10-31"}'
curl $H/api/v1/acme/stats -H "X-Admin-Key: ak_..."
```
Promotions: `discount_pct` OR `discount_amount`, optional `category` / `product_id` / `code` / `starts_on` / `ends_on`.
A perk with no discount (e.g. "Free delivery this week") is just a title + description. Offers never stack; expired ones vanish automatically.

## Operate
- Logs: `docker compose logs -f app`   Update: `git pull && docker compose up -d --build`
- Backup (daily via cron):
```
docker compose exec -T app python -c "import sqlite3;s=sqlite3.connect('/data/agent.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
docker compose cp app:/data/backup.db ./backups/agent-$(date +%F).db
```
- Keep ONE app instance: SQLite and the in-memory rate limiter are per-process. That comfortably serves many small shops; move to Postgres/Redis when you outgrow it.
- Set a spending alert on the LLM account; the per-IP and per-session rate limits already cap abuse.

## Hardening notes (what changed, what to set)
- **Production mode** (`ENV=production`, set by docker-compose): the app refuses to start unless `SUPER_ADMIN_KEY` is 24+ random chars and `LLM_API_KEY` is set. API docs are hidden.
- **WhatsApp webhook**: in production it is DISABLED until `WHATSAPP_APP_SECRET` is set (Meta app dashboard > App settings > Basic). Unsigned requests are always rejected once it is set.
- **`SECRETS_KEY`** (required to store WhatsApp tokens): `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Back this key up separately from the database; without it stored tokens cannot be read.
- **Daily cap**: each client has `daily_message_limit` (default 300 customer messages/day) so one busy or abused client cannot drain your shared LLM quota. Change with `PUT /config`. Usage shows in `/stats`.
- **Fallback model**: set `LLM_FALLBACK_*` (a cheap paid model) and the bot switches to it automatically when the free one fails or is rate-limited.
- **Lost or leaked key**: `POST /admin/tenants/{id}/reset-keys` with your super key issues new keys; clients can self-rotate their admin key at `POST /api/v1/{id}/rotate-admin-key`. Repeated wrong keys lock that IP out for 5 minutes.
- **Single product edits** (no re-upload): `PUT /products/{id}` (add/replace), `PATCH /products/{id}` with `{"price":..,"stock_count":..}`, `DELETE /products/{id}`.
- **Privacy**: `DELETE /api/v1/{id}/customers?phone=0712345678` (or `&email=`) erases that person's leads, bookings, handoffs and WhatsApp chat. `DELETE /conversations/{session}` removes one chat (session ids look like `web:abc` or `wa:2547...`). Chat history is auto-purged after `RETENTION_MESSAGE_DAYS` (90), logs after `RETENTION_LOG_DAYS` (30).
- **Catalog uploads are all-or-nothing**: the new index is built first and only goes live when the database commits; a failed upload leaves the old catalog serving.
- **Upgrading an existing install**: the database upgrades itself, but re-upload each client's catalog once (the index format changed).

## Testing
```
python tests/test_hardening.py        # offline security/robustness suite (no keys needed)
python tests/live_check.py http://127.0.0.1:8000 demo pk_xxx   # real model, real chats: read the replies
```
