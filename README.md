# Business AI Agent (multi-tenant, free stack)

LLM: any OpenAI-compatible API (default Groq free tier) · Embeddings: local/free (Chroma ONNX) · DB: SQLite

## Run locally
```
pip install -r requirements.txt
test -f .env || cp .env.example .env   # keep an existing .env and its API keys unchanged
# If starting from .env.example, set LLM_API_KEY and SUPER_ADMIN_KEY in .env
python seed_demo.py           # creates demo + sample catalog on first use; preserves an existing demo catalog
uvicorn app:app --reload
```
Open the printed `http://127.0.0.1:8000/widget?t=demo&k=<public_key>` URL. Keep the server running in its terminal; the widget sends messages to that same server. To embed it on a site, use the iframe example below. For a command-line chat instead, run `python chat_cli.py demo <public_key>`.

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

## Try the bundled Amazon sample inventory
The included `furniture_products_dataset_from_amazon_sample.csv` is source data, not yet in the app's upload format. Convert it locally (no Kaggle download or extra packages needed):
```
python tools/amazon_sample_to_catalog.py
```

Create a separate test tenant so this sample does not replace the demo or a customer's catalog. Use the existing `SUPER_ADMIN_KEY` from `.env` (make it available to this shell if it is not already exported; do not replace the key):
```
curl -X POST http://localhost:8000/admin/tenants \
  -H "X-Super-Key: $SUPER_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"amazon-test","business_name":"Amazon Sample Test","config":{"currency":"USD"}}'
```
Save the returned `admin_key` and `widget_path`, then upload the converted CSV using the returned admin key:
```
curl -X POST http://localhost:8000/api/v1/amazon-test/catalog \
  -H "X-Admin-Key: ak_RETURNED_ADMIN_KEY" -F file=@amazon_sample_catalog.csv
```
Open `http://localhost:8000` followed by the returned `widget_path` (for example `/widget?t=amazon-test&k=pk_RETURNED_PUBLIC_KEY`) to chat against the test inventory. In the widget, ask about products, categories, and prices. Prices are left in USD. The CSV's availability is a snapshot, not live stock: explicit counts are retained, “In Stock” is represented as a minimum of one, and unavailable items as zero. Use the admin product PATCH endpoint to test stock changes.

Repeated ASIN rows are deduplicated, and rows with missing prices are skipped rather than assigned made-up prices. The converter writes `amazon_sample_catalog.csv`; it is ignored by git. The generic Kaggle/local CSV converter remains available separately:
```
pip install -r requirements-tools.txt
python tools/kaggle_to_catalog.py --rate 130 --round-to 100
```

Lost demo keys? With the server running:
`curl -X POST http://localhost:8000/admin/tenants/demo/reset-keys -H "X-Super-Key: <SUPER_ADMIN_KEY from .env>"` (issues new keys, so use the new pk_ in the widget URL).
