#!/usr/bin/env python3
"""Convert the bundled Amazon furniture sample CSV into the inventory upload format."""
import argparse
import ast
import csv
import io
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "furniture_products_dataset_from_amazon_sample.csv"
DEFAULT_OUTPUT = ROOT / "amazon_sample_catalog.csv"
FIELDS = [
    "id", "name", "category", "price", "stock_count", "description", "brand",
    "color", "material", "style", "primary_image", "url", "availability",
]


def _text(value: str | None) -> str:
    return (value or "").strip()


def _category(value: str) -> str:
    value = _text(value)
    if value.startswith("["):
        try:
            categories = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
        if isinstance(categories, (list, tuple)):
            items = [str(item).strip() for item in categories if str(item).strip()]
            if items:
                return items[-1]
    return value


def _price(value: str) -> float | None:
    cleaned = re.sub(r"[^0-9.\-]", "", _text(value).replace(",", ""))
    try:
        price = float(cleaned)
    except ValueError:
        return None
    return price if math.isfinite(price) and 0 < price <= 1_000_000_000 else None


def _stock(value: str) -> int:
    value = _text(value)
    if re.search(r"\b(out of stock|unavailable|sold out)\b", value, re.IGNORECASE):
        return 0
    quantity = re.search(r"\b(\d+)\b", value)
    if quantity:
        return min(int(quantity.group(1)), 1_000_000)
    # "In Stock" only establishes a minimum quantity, not the seller's actual count.
    return 1


def convert_csv(source: str) -> tuple[str, dict[str, int]]:
    reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")))
    required = {"asin", "title", "price"}
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"Amazon sample is missing columns: {', '.join(sorted(missing))}")

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    stats = {
        "source_rows": 0,
        "products": 0,
        "skipped_missing_title": 0,
        "skipped_missing_price": 0,
        "skipped_duplicate_id": 0,
    }
    seen_ids: set[str] = set()

    for row_number, raw in enumerate(reader, start=2):
        stats["source_rows"] += 1
        name = _text(raw.get("title"))
        if not name:
            stats["skipped_missing_title"] += 1
            continue
        price = _price(raw.get("price", ""))
        if price is None:
            stats["skipped_missing_price"] += 1
            continue
        product_id = _text(raw.get("asin")) or f"amazon-sample-{row_number:05d}"
        if product_id in seen_ids:
            stats["skipped_duplicate_id"] += 1
            continue
        seen_ids.add(product_id)

        description_parts = [
            _text(raw.get("description")),
            _text(raw.get("about_item")),
            _text(raw.get("product_overview")),
            _text(raw.get("important_information")),
        ]
        description = " ".join(part for part in description_parts if part)
        writer.writerow({
            "id": product_id,
            "name": name,
            "category": _category(raw.get("categories", "")),
            "price": f"{price:.2f}",
            "stock_count": _stock(raw.get("availability", "")),
            "description": description,
            "brand": _text(raw.get("brand")),
            "color": _text(raw.get("color")),
            "material": _text(raw.get("material")),
            "style": _text(raw.get("style")),
            "primary_image": _text(raw.get("primary_image")),
            "url": _text(raw.get("url")),
            "availability": _text(raw.get("availability")),
        })
        stats["products"] += 1

    if not stats["products"]:
        raise ValueError("No products with both a title and a valid price were found")
    return output.getvalue(), stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_SOURCE, help="Amazon sample CSV to convert")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="catalog CSV output path")
    args = parser.parse_args()

    catalog_csv, stats = convert_csv(args.csv.read_text(encoding="utf-8-sig"))
    import sys

    sys.path.insert(0, str(ROOT))
    import catalog

    accepted = len(catalog.parse_csv(catalog_csv))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(catalog_csv, encoding="utf-8")
    print(f"Converted {stats['products']} products from {stats['source_rows']} source rows.")
    print(f"Skipped {stats['skipped_missing_title']} rows without a title and "
          f"{stats['skipped_missing_price']} rows without a usable price, plus "
          f"{stats['skipped_duplicate_id']} repeated product IDs.")
    print(f"Validated {accepted} products; wrote {args.out}")
    print("Prices are preserved in USD. Stock reflects the sample's availability text, "
          "not live seller inventory.")


if __name__ == "__main__":
    main()
