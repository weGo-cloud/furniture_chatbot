#!/usr/bin/env python3
"""Turn a Kaggle (or any) furniture CSV into the catalog CSV this bot expects.

  pip install kagglehub pandas
  python tools/kaggle_to_catalog.py                       # downloads rajagrawal7089/furniture-sales-data
  python tools/kaggle_to_catalog.py --csv mydata.csv      # or convert a CSV you already have
  python tools/kaggle_to_catalog.py --rate 130 --round-to 100   # e.g. USD -> KES, prices to nearest 100

It guesses which columns are name / price / category / description, collapses repeated order rows into one
row per product, makes up STOCK numbers if the data has none (so out-of-stock behaviour can be tested),
and writes catalog.csv. If it cannot find a name or price column it prints the columns so you can adjust."""
import argparse
import glob
import os
import random
import re
import sys
from pathlib import Path

import pandas as pd

CANDIDATES = {  # normalised column names, in priority order
    "name": ["productname", "producttitle", "title", "itemname", "furniturename", "product", "item", "name"],
    "id": ["productid", "sku", "itemid", "articleid", "uniqueid", "id"],
    "category": ["productcategory", "category", "maincategory", "furnituretype", "type"],
    "subcategory": ["productsubcategory", "subcategory", "subcat"],
    "price": ["productprice", "unitprice", "sellingprice", "saleprice", "currentprice", "price", "retailprice", "mrp"],
    "sales": ["sales", "revenue", "totalsales", "totalamount", "totalprice"],
    "quantity": ["quantity", "qty", "unitssold", "orderquantity"],
    "description": ["productdescription", "shortdescription", "description", "details", "features", "about"],
    "stock": ["stockcount", "stockquantity", "quantityinstock", "instock", "stock", "inventory", "available"],
    "brand": ["brand", "manufacturer", "make"],
}
EXTRAS = ["material", "color", "colour", "dimensions", "size", "style", "room", "rating", "warranty",
          "width", "height", "depth", "weight", "finish"]


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")


def text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().replace({"nan": "", "None": ""})


def read_any(path: str) -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"Could not read {path} as text CSV")


def fetch(slug: str, pick: str | None) -> str:
    import kagglehub  # imported late so --csv works without it
    path = kagglehub.dataset_download(slug)
    files = sorted(glob.glob(os.path.join(path, "**", "*.csv"), recursive=True), key=os.path.getsize, reverse=True)
    print(f"Downloaded to {path}")
    if not files:
        raise SystemExit(f"No .csv files in {path}. Contents: {os.listdir(path)}")
    for f in files:
        print(f"  found: {os.path.basename(f)} ({os.path.getsize(f):,} bytes)")
    if pick:
        match = [f for f in files if os.path.basename(f) == pick]
        if not match:
            raise SystemExit(f"--file {pick} not among the files above")
        return match[0]
    print(f"Using the largest: {os.path.basename(files[0])} (choose another with --file NAME.csv)")
    return files[0]


def convert(df: pd.DataFrame, a) -> tuple[pd.DataFrame, dict]:
    m = {norm(c): c for c in df.columns}
    col = {k: next((m[n] for n in names if n in m), None) for k, names in CANDIDATES.items()}

    if col["price"]:
        price = to_num(df[col["price"]])
    elif col["sales"] and col["quantity"]:
        price = to_num(df[col["sales"]]) / to_num(df[col["quantity"]]).replace(0, float("nan"))
    elif col["sales"]:
        price = to_num(df[col["sales"]])
    else:
        raise SystemExit(f"No price column found. Columns are:\n  {list(df.columns)}")

    cat = text(df[col["category"]]) if col["category"] else pd.Series("Furniture", index=df.index)
    sub = text(df[col["subcategory"]]) if col["subcategory"] else None
    brand = text(df[col["brand"]]) if col["brand"] else None
    if col["name"]:
        name = text(df[col["name"]])
    elif sub is not None or col["category"]:  # order-style data without product names: build "Brand Subcategory"
        parts = [s for s in (brand, sub if sub is not None else cat) if s is not None]
        name = parts[0] if len(parts) == 1 else (parts[0] + " " + parts[1]).str.strip()
    else:
        raise SystemExit(f"No product name/category column found. Columns are:\n  {list(df.columns)}")

    w = pd.DataFrame({"name": name, "category": cat, "price": price})
    if sub is not None:
        w["subcategory"] = sub
    if brand is not None:
        w["brand"] = brand
    for e in EXTRAS:
        if e in m:
            w[e] = text(df[m[e]])
    w["description"] = text(df[col["description"]]) if col["description"] else ""
    w["stock_count"] = to_num(df[col["stock"]]) if col["stock"] else float("nan")
    w["id"] = text(df[col["id"]]) if col["id"] else ""

    w = w[(w["name"] != "") & w["price"].notna() & (w["price"] > 0)]
    if w.empty:
        raise SystemExit(f"No usable rows (need a name and a price > 0). Columns are:\n  {list(df.columns)}")

    first = lambda s: s.replace("", pd.NA).dropna().iloc[0] if s.replace("", pd.NA).notna().any() else ""  # noqa: E731
    agg = {c: (first if w[c].dtype == object else "first") for c in w.columns if c not in ("name", "price")}
    agg["price"] = "median"
    w = w.groupby("name", sort=False).agg(agg).reset_index()

    if len(w) > a.limit:
        w = w.sample(a.limit, random_state=a.seed).reset_index(drop=True)

    ids_ok = (w["id"] != "").all() and w["id"].is_unique
    if not ids_ok:
        w["id"] = [f"{a.prefix}{i:04d}" for i in range(1, len(w) + 1)]
    w["price"] = ((w["price"] * a.rate / a.round_to).round() * a.round_to).clip(lower=a.round_to).astype(int)

    rng = random.Random(a.seed)

    def fake_stock(_):
        r = rng.random()
        return 0 if r < 0.10 else rng.randint(1, 3) if r < 0.25 else rng.randint(4, 25)

    has_real = w["stock_count"].notna()
    w["stock_count"] = [int(max(s, 0)) if ok else fake_stock(None) for s, ok in zip(w["stock_count"].fillna(0), has_real)]

    def synth(r):
        attrs = [str(r[k]) for k in ("brand", "material", "color", "colour", "style", "finish", "subcategory")
                 if k in r and str(r[k]).strip() and str(r[k]) != r["name"]]
        bits = [r["name"]] + ([", ".join(attrs)] if attrs else []) + ([f"for the {r['category']}"] if r["category"] else [])
        return " - ".join(bits)

    w["description"] = [d if str(d).strip() else synth(r) for d, (_, r) in zip(w["description"], w.iterrows())]
    lead = ["id", "name", "category", "price", "stock_count", "description"]
    w = w[lead + [c for c in w.columns if c not in lead]].fillna("")
    return w, {k: v for k, v in col.items() if v}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", help="convert this local CSV instead of downloading")
    p.add_argument("--slug", default="rajagrawal7089/furniture-sales-data")
    p.add_argument("--file", help="which CSV inside the Kaggle download to use")
    p.add_argument("--out", default="catalog.csv")
    p.add_argument("--limit", type=int, default=300, help="max products (default 300; keeps indexing quick)")
    p.add_argument("--rate", type=float, default=1.0, help="multiply prices, e.g. 130 for USD->KES")
    p.add_argument("--round-to", type=int, default=1, dest="round_to", help="round prices to nearest N, e.g. 100")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--prefix", default="P")
    a = p.parse_args()

    path = a.csv or fetch(a.slug, a.file)
    df = read_any(path)
    print(f"\nRead {len(df):,} rows. Columns: {list(df.columns)}")
    out, used = convert(df, a)
    out.to_csv(a.out, index=False, encoding="utf-8")
    print(f"\nColumns I matched: {used}")
    print(f"Wrote {len(out)} products to {a.out}")
    print("Categories:", {k: int(v) for k, v in out['category'].value_counts().head(8).items()})
    print(out[["id", "name", "category", "price", "stock_count"]].head(5).to_string(index=False))
    print("NOTE: stock numbers are random unless your data had a stock column. This is TEST data.")
    try:  # self-check with the bot's own validator
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import catalog
        n = len(catalog.parse_csv(Path(a.out).read_text(encoding="utf-8")))
        print(f"Validated: the bot will accept all {n} rows.")
    except ImportError:
        print("(Skipped validation: run from the project folder with its requirements installed.)")
    except ValueError as e:
        raise SystemExit(f"The bot would reject this file: {e}")


if __name__ == "__main__":
    main()
