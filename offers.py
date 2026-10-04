"""Offer math lives in code, never in the LLM, so quoted prices are always exact."""


def _applies(pr: dict, p: dict) -> bool:
    if pr["product_id"]:
        return pr["product_id"] == p["id"]
    cat = (pr["category"] or "").lower()
    if cat:
        return cat in (p["category"] or "").lower() or cat in p["name"].lower()
    return True


def _saving(pr: dict, p: dict) -> float:
    s = p["price"] * pr["discount_pct"] / 100 if pr["discount_pct"] else pr["discount_amount"]
    return round(min(max(s, 0), p["price"]), 2)


def best_offer(p: dict, promos: list[dict]) -> dict | None:
    """Single best applicable offer for a product (offers never stack)."""
    applicable = [pr for pr in promos if _applies(pr, p)]
    if not applicable:
        return None
    best = max(applicable, key=lambda pr: _saving(pr, p))
    s = _saving(best, p)
    return {"title": best["title"], "description": best["description"], "saving": s,
            "final": p["price"] - s, "ends_on": best["ends_on"], "code": best["code"]}


def offer_line(o: dict, currency: str) -> str:
    if o["saving"] > 0:
        line = f"Offer: {o['title']} - now {currency} {o['final']:,.0f} (save {currency} {o['saving']:,.0f})"
    else:
        line = f"Offer: {o['title']}" + (f" - {o['description']}" if o["description"] else "")
    if o["ends_on"]:
        line += f", valid until {o['ends_on']}"
    if o["code"]:
        line += f", code {o['code']}"
    return line


def promo_text(pr: dict, currency: str) -> str:
    parts = [pr["title"]]
    if pr["discount_pct"]:
        parts.append(f"{pr['discount_pct']:g}% off")
    elif pr["discount_amount"]:
        parts.append(f"{currency} {pr['discount_amount']:,.0f} off")
    if pr["description"]:
        parts.append(pr["description"])
    parts.append("applies to: " + (f"product {pr['product_id']}" if pr["product_id"] else pr["category"] or "whole catalog"))
    if pr["ends_on"]:
        parts.append(f"until {pr['ends_on']}")
    if pr["code"]:
        parts.append(f"code {pr['code']}")
    return " | ".join(parts)
