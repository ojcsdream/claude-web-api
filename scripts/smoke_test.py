#!/usr/bin/env python3
import json
import sys
import time
import urllib.parse
import urllib.request


BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def fetch_json(path: str):
    with urllib.request.urlopen(BASE_URL + path, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def assert_ok(name: str, condition: bool, detail: str = ""):
    if not condition:
        raise SystemExit(f"FAIL {name}: {detail}")
    print(f"OK {name}")


def main():
    health = fetch_json("/api/health")
    assert_ok("health", bool(health.get("ok")), str(health))

    conversations = fetch_json("/api/conversations")
    assert_ok("conversations", conversations.get("ok") is True and isinstance(conversations.get("conversations"), list))

    query = urllib.parse.urlencode({"q": "Claude", "limit": "5"})
    search = fetch_json("/api/search?" + query)
    assert_ok("global_search", search.get("ok") is True and isinstance(search.get("results"), list))

    convs = conversations.get("conversations") or []
    if convs:
        cid = convs[0].get("id", "")
        query = urllib.parse.urlencode({
            "q": convs[0].get("title") or "Claude",
            "scope": "conversation",
            "conversation_id": cid,
            "limit": "5",
        })
        scoped = fetch_json("/api/search?" + query)
        assert_ok("conversation_search", scoped.get("ok") is True and scoped.get("scope") == "conversation")

        export_path = f"/api/conversations/{urllib.parse.quote(cid)}/export.md"
        with urllib.request.urlopen(BASE_URL + export_path, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        assert_ok("markdown_export", len(text) > 0)

    print("smoke_test_passed", int(time.time()))


if __name__ == "__main__":
    main()
