"""Catalog import (CSV / single products) + hybrid search.

Embeddings run locally and free via Chroma's built-in ONNX MiniLM model (no API key).
SQLite is the source of truth for price/stock. The vector index is VERSIONED: a new upload is built
under a new collection name and only becomes active when SQLite commits the new version number,
so a crash mid-upload can never leave search half-updated."""
import csv
import io
import math
import re
import threading

import chromadb
from chromadb.utils import embedding_functions

import db
import offers
import settings

REQUIRED = {"id", "name", "price", "stock_count"}
CORE = {"id", "name", "category", "price", "stock_count"}
NOT_EMBEDDED = {"id", "price", "stock_count"}  # numbers live in SQLite; keeping them out means no re-embedding on price/stock edits
MAX_ROWS = 5000
MAX_CELL = 2000

_client = None
_ef = None
_lock = threading.Lock()


def _chroma():
    global _client, _ef
    with _lock:
        if _client is None:
            _client = chromadb.PersistentClient(path=str(settings.DATA_DIR / "chroma"))
            _ef = embedding_functions.DefaultEmbeddingFunction()
    return _client


def _cname(tenant_id: str, version: int) -> str:
    return f"inv_{tenant_id}_v{version}"


def _col(tenant_id: str, version: int):
    return _chroma().get_or_create_collection(
        _cname(tenant_id, version), embedding_function=_ef, metadata={"hnsw:space": "cosine"})


def clean(v) -> str:
    """Neutralise uploaded text: no control chars, no angle brackets (so data can't break out of
    the <catalog_data> wrapper the agent sees), bounded length."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).replace("<", "").replace(">", "")
    return re.sub(r" +", " ", s).strip()[:MAX_CELL]


def build_row(raw: dict, label: str) -> dict:
    r = {clean(k).lower(): clean(v) for k, v in raw.items() if k}
    if not r.get("id") or not r.get("name"):
        raise ValueError(f"{label}: id and name are required")
    if len(r["id"]) > 60 or len(r["name"]) > 200:
        raise ValueError(f"{label}: id (max 60) or name (max 200) is too long")
    try:
        price = float(r.get("price", "").replace(",", ""))
        stock = int(float(r.get("stock_count") or 0))
    except (ValueError, OverflowError):
        raise ValueError(f"{label}: price and stock_count must be numbers")
    if not math.isfinite(price) or not 0 <= price <= 1e9:
        raise ValueError(f"{label}: price must be between 0 and 1,000,000,000")
    if not 0 <= stock <= 1_000_000:
        raise ValueError(f"{label}: stock_count must be between 0 and 1,000,000")
    r["price"], r["stock_count"] = f"{price:g}", str(stock)
    return {"id": r["id"], "name": r["name"], "category": r.get("category", ""),
            "price": price, "stock": stock, "data": r}


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    cols = {clean(c).lower() for c in (reader.fieldnames or [])}
    if REQUIRED - cols:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(REQUIRED - cols))}. "
                         "Required: id,name,price,stock_count (optional: category, description, anything else).")
    rows, seen = [], set()
    for i, raw in enumerate(reader, start=2):
        row = build_row(raw, f"Row {i}")
        if row["id"] in seen:
            raise ValueError(f"Row {i}: duplicate id {row['id']}")
        seen.add(row["id"])
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Too many rows (max {MAX_ROWS})")
    if not rows:
        raise ValueError("CSV has no product rows")
    return rows


def _doc_text(row: dict) -> str:
    return ". ".join(f"{k}: {v}" for k, v in row["data"].items() if v and k not in NOT_EMBEDDED)


def index_csv(tenant_id: str, text: str) -> int:
    """Replace the tenant's whole catalog. All-or-nothing: on any failure the old catalog keeps serving."""
    rows = parse_csv(text)
    old = db.catalog_version(tenant_id)
    new = old + 1
    client = _chroma()
    try:
        client.delete_collection(_cname(tenant_id, new))  # leftover from a crashed earlier attempt
    except Exception:
        pass
    col = _col(tenant_id, new)
    try:
        for i in range(0, len(rows), 200):
            batch = rows[i:i + 200]
            col.upsert(ids=[r["id"] for r in batch], documents=[_doc_text(r) for r in batch],
                       metadatas=[{"pid": r["id"]} for r in batch])
    except Exception:
        try:
            client.delete_collection(_cname(tenant_id, new))
        except Exception:
            pass
        raise
    db.replace_products(tenant_id, rows, version=new)  # <- the atomic switch
    if old > 0:
        try:
            client.delete_collection(_cname(tenant_id, old))
        except Exception:
            pass
    return len(rows)


def upsert_product(tenant_id: str, raw: dict) -> dict:
    row = build_row(raw, "Product")
    ver = db.catalog_version(tenant_id)
    first = ver == 0
    ver = ver or 1
    _col(tenant_id, ver).upsert(ids=[row["id"]], documents=[_doc_text(row)], metadatas=[{"pid": row["id"]}])
    db.upsert_product(tenant_id, row)  # embed first: if embedding fails, SQL is untouched
    if first:
        db.set_catalog_version(tenant_id, 1)
    return row


def patch_product(tenant_id: str, pid: str, price: float | None, stock: int | None) -> None:
    """Price/stock live only in SQLite, so this needs no re-embedding."""
    if not db.update_product_fields(tenant_id, pid, price, stock):
        raise KeyError(pid)


def delete_product(tenant_id: str, pid: str) -> None:
    if not db.delete_product(tenant_id, pid):
        raise KeyError(pid)
    ver = db.catalog_version(tenant_id)
    if ver:
        try:
            _col(tenant_id, ver).delete(ids=[pid])
        except Exception:
            pass  # a stale vector is harmless: results are resolved through SQLite


def search(tenant_id: str, query: str = "", n: int = 5, category: str = "", min_price: float | None = None,
           max_price: float | None = None, in_stock_only: bool = False) -> tuple[list[dict], int]:
    """Hybrid search: exact filters in SQLite, then semantic ranking of the survivors in Chroma.
    No query -> plain filtered browse sorted by price. Returns (top n items, total matches)."""
    n = max(1, min(int(n or 5), 10))
    cands = db.filter_products(tenant_id, category, min_price, max_price, in_stock_only)
    total = len(cands)
    ver = db.catalog_version(tenant_id)
    if total <= 1 or not (query or "").strip() or ver == 0:
        return cands[:n], total
    by_id = {p["id"]: p for p in cands}
    filtered = bool(category or min_price is not None or max_price is not None or in_stock_only)
    col = _col(tenant_id, ver)
    size = col.count()
    if size == 0:
        return cands[:n], total
    where = {"pid": {"$in": list(by_id)}} if filtered and total <= 2000 else None
    res = col.query(query_texts=[query], n_results=min(n if where else max(n, 100), size), where=where)
    ranked = [by_id[i] for i in res["ids"][0] if i in by_id]
    return (ranked or cands)[:n], total


def format_product(p: dict, currency: str, promos: list[dict] | None = None) -> str:
    stock = p["stock"]
    status = "OUT OF STOCK" if stock <= 0 else (f"only {stock} left" if stock <= 3 else f"{stock} in stock")
    head = f"[{p['id']}] {p['name']}"
    if p["category"]:
        head += f" | {p['category']}"
    head += f" | {currency} {p['price']:,.2f} | {status}"
    extras = "; ".join(f"{k}: {v}" for k, v in p["data"].items() if k not in CORE and v)
    lines = [head] + ([extras] if extras else [])
    offer = offers.best_offer(p, promos) if promos else None
    if offer and stock > 0:
        lines.append(offers.offer_line(offer, currency))
    return "\n".join(lines)
