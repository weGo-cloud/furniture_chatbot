# Business AI Agent (multi-tenant, free stack)

LLM: any OpenAI-compatible API (default Groq free tier) · Embeddings: local/free (Chroma ONNX) · DB: SQLite

## Run locally
```
pip install -r requirements.txt
cp .env.example .env          # set LLM_API_KEY and SUPER_ADMIN_KEY
python seed_demo.py           # creates tenant "demo" + sample catalog, prints keys + widget URL
uvicorn app:app --reload
```
Open the printed `/widget?t=demo&k=<public_key>` URL, or `python chat_cli.py demo <public_key>`.

## Onboard a client
```
# 1. create tenant (you)
curl -X POST localhost:8000/admin/tenants -H "X-Super-Key: $SUPER_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme-furniture","business_name":"Acme Furniture","config":{"owner_email":"owner@acme.co.ke","allowed_origins":["https://acme.co.ke"]}}'
# 2. upload their catalog (CSV: id,name,price,stock_count required; category,description,anything else optional)
curl -X POST localhost:8000/api/v1/acme-furniture/catalog -H "X-Admin-Key: ak_..." -F file=@catalog.csv
# 3. embed on their site
<iframe src="https://YOUR-HOST/widget?t=acme-furniture&k=pk_..." style="width:380px;height:560px;border:0"></iframe>
```
Client/admin endpoints (header `X-Admin-Key`): `PUT /config`, `GET /leads`, `/bookings`, `/handoffs`, `/conversations/{session}`.
Config keys: persona, timezone, currency, slot_times, open_days (Mon=0), appointment_minutes, owner_email, allowed_origins, whatsapp.

## WhatsApp (Meta Cloud API)
1. developers.facebook.com → create app → add WhatsApp → note `phone_number_id` + a permanent access token.
2. Webhook URL: `https://YOUR-HOST/webhooks/whatsapp`, verify token = `WHATSAPP_VERIFY_TOKEN`; subscribe to `messages`. Set `WHATSAPP_APP_SECRET` to enable signature checks.
3. `PUT /api/v1/<tenant>/config` with `{"whatsapp":{"phone_number_id":"...","token":"..."}}`.

## Docker
```
docker build -t agent .
docker run -p 8000:8000 --env-file .env -v agent-data:/data agent
```
Mount a volume at /data: it holds the SQLite DB and vector index. Without one, data is lost on redeploy.

## Sales behaviour
See DEPLOY.md for promotions, business_facts and the production setup. Offers are priced in code and quoted exactly; the bot only mentions real offers, real stock levels and real policies you supply.

Run `python tests/test_hardening.py` after any change. See DEPLOY.md for production settings.

## Test with a Kaggle dataset
```
pip install kagglehub pandas
python tools/kaggle_to_catalog.py --rate 130 --round-to 100      # downloads + converts -> catalog.csv
curl -X POST http://localhost:8000/api/v1/demo/catalog -H "X-Admin-Key: ak_YOURKEY" -F file=@catalog.csv
```
Then chat at `/widget?t=demo&k=pk_YOURKEY` (or `python chat_cli.py demo pk_YOURKEY`). Lost your keys? With the server running:
`curl -X POST http://localhost:8000/admin/tenants/demo/reset-keys -H "X-Super-Key: <SUPER_ADMIN_KEY from .env>"` (issues new keys, so use the new pk_ in the widget URL).
