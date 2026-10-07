"""Creates a 'demo' tenant and loads sample_products.csv:  python seed_demo.py"""
from pathlib import Path

import catalog
import db

db.init()
t = db.get_tenant("demo")
if t:
    print("Tenant 'demo' already exists; leaving its catalog unchanged (admin key is not recoverable).")
    pk = t["public_key"]
    print(f"Open: http://127.0.0.1:8000/widget?t=demo&k={pk}")
else:
    pk, ak = db.create_tenant("demo", "Demo Furniture Co")
    print(f"Created tenant 'demo'\n  public_key: {pk}\n  admin_key : {ak}   <- save this now")
    n = catalog.index_csv("demo", Path("sample_products.csv").read_text(encoding="utf-8"))
    print(f"Indexed {n} products.\nStart the server, then open: http://127.0.0.1:8000/widget?t=demo&k={pk}")
