"""Terminal client: python chat_cli.py <tenant_id> <public_key>   (server must be running)"""
import json
import sys
import urllib.request
import uuid

tenant, key = sys.argv[1], sys.argv[2]
sid = "cli-" + uuid.uuid4().hex[:8]
while True:
    msg = input("\nYou: ").strip()
    if not msg:
        continue
    req = urllib.request.Request(
        f"http://127.0.0.1:8000/api/v1/{tenant}/chat",
        data=json.dumps({"session_id": sid, "message": msg}).encode(),
        headers={"Content-Type": "application/json", "X-Tenant-Key": key})
    with urllib.request.urlopen(req) as r:
        print("Bot:", json.loads(r.read())["response"])
