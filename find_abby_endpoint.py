# -*- coding: utf-8 -*-
"""
Abby endpoint finder.
Tries common chat-completion paths and auth headers, and looks for an OpenAPI spec.
Any status other than 404 means the route exists. Copy the winning values into .env.
Run:  python find_abby_endpoint.py
"""
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

KEY = (os.getenv("ABBY_API_KEY") or "").strip()
BASE = (os.getenv("ABBY_BASE_URL") or "https://api.abby.abb.com").rstrip("/")
MODEL = (os.getenv("ABBY_MODEL") or "gemini-2.5-flash").strip()
VERIFY = (os.getenv("ABBY_VERIFY_SSL") or "true").lower() in {"1", "true", "yes", "on"}

if not KEY:
    raise SystemExit("ABBY_API_KEY missing in .env")

SPEC_PATHS = [
    "/openapi.json", "/swagger.json", "/v1/openapi.json", "/api/openapi.json",
    "/documentation/openapi.json", "/docs/openapi.json", "/swagger/v1/swagger.json",
    "/documentation/", "/docs",
]

CHAT_PATHS = [
    "/v1/chat/completions", "/chat/completions", "/api/v1/chat/completions",
    "/api/chat/completions", "/openai/v1/chat/completions", "/v1/chat", "/chat",
    "/api/chat", "/api/v1/chat", "/v1/completions", "/completions",
    "/v1/chat-completion", "/chat-completion", "/api/chat-completion",
    "/v1/chat_completion", "/chat_completion", "/v1/stateless/chat",
    "/stateless/chat", "/v1/models", "/models", "/api/v1/models",
]

AUTH_VARIANTS = [
    ("Authorization", "Bearer"),
    ("x-api-key", ""),
    ("Ocp-Apim-Subscription-Key", ""),
    ("api-key", ""),
]

BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Reply with the word OK."}],
    "max_tokens": 10,
}


def headers(name, scheme):
    value = f"{scheme} {KEY}".strip() if scheme else KEY
    return {name: value, "Content-Type": "application/json", "Accept": "application/json"}


def short(text, n=160):
    return " ".join(text.split())[:n]


with httpx.Client(timeout=30, verify=VERIFY, follow_redirects=True) as client:
    print(f"Base URL: {BASE}\nModel: {MODEL}\n")

    print("=== 1. Looking for API documentation / OpenAPI spec ===")
    for path in SPEC_PATHS:
        try:
            r = client.get(BASE + path, headers=headers("Authorization", "Bearer"))
            print(f"{r.status_code}  GET {path}  {short(r.text, 100)}")
            if r.status_code == 200 and "paths" in r.text[:50000]:
                try:
                    spec = r.json()
                    print("\n  OpenAPI paths found:")
                    for p, ops in spec.get("paths", {}).items():
                        print(f"    {', '.join(m.upper() for m in ops)}  {p}")
                    schemes = spec.get("components", {}).get("securitySchemes", {})
                    if schemes:
                        print("  Security schemes:", json.dumps(schemes, indent=2))
                except Exception:
                    pass
        except Exception as exc:
            print(f"ERR  GET {path}  {exc}")

    print("\n=== 2. Probing chat paths (non-404 = route exists) ===")
    hits = []
    for path in CHAT_PATHS:
        method = "GET" if path.endswith("models") else "POST"
        try:
            r = client.request(method, BASE + path, headers=headers("Authorization", "Bearer"),
                               json=None if method == "GET" else BODY)
            mark = "" if r.status_code == 404 else "   <-- route exists"
            print(f"{r.status_code}  {method} {path}{mark}")
            if r.status_code != 404:
                hits.append((method, path))
                print(f"      {short(r.text)}")
        except Exception as exc:
            print(f"ERR  {method} {path}  {exc}")

    if hits:
        print("\n=== 3. Testing auth headers on found routes ===")
        for method, path in hits:
            for name, scheme in AUTH_VARIANTS:
                try:
                    r = client.request(method, BASE + path, headers=headers(name, scheme),
                                       json=None if method == "GET" else BODY)
                    ok = "   <-- WORKS" if r.status_code == 200 else ""
                    print(f"{r.status_code}  {method} {path}  [{name}: {scheme or '<key>'}]{ok}")
                    if r.status_code == 200:
                        print(f"      {short(r.text, 400)}")
                except Exception as exc:
                    print(f"ERR  {path} [{name}]  {exc}")
    else:
        print("\nNo chat route found. Open https://api.abby.abb.com/documentation/ in your browser")
        print("(on ABB network/VPN), go to API Reference, and copy the exact URL, header and body.")