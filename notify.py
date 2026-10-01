"""
notify.py — push a suggestion to a phone through ntfy.

One HTTP POST per message. NTFY_URL defaults to the public ntfy.sh; point it
at a self-hosted server to keep everything on your own network. NTFY_TOPIC is
the channel the phone app subscribes to — treat it like a password when using
the public server. NTFY_TOKEN is only needed on a server with access control.
"""
from __future__ import annotations

import os

PRIORITY = {"close": "high", "csp": "default", "cc": "default", "note": "low"}
TAGS = {"close": "white_check_mark", "csp": "moneybag", "cc": "phone", "note": "warning"}


def config() -> dict:
    return {
        "url": (os.environ.get("NTFY_URL") or "https://ntfy.sh").rstrip("/"),
        "topic": (os.environ.get("NTFY_TOPIC") or "").strip(),
        "token": (os.environ.get("NTFY_TOKEN") or "").strip(),
        "app_url": (os.environ.get("APP_URL") or "").rstrip("/"),
    }


def push(s: dict, cfg: dict | None = None, timeout: float = 15.0) -> bool:
    """Send one suggestion. Returns True on a 2xx. Never raises."""
    cfg = cfg or config()
    if not cfg["topic"]:
        return False
    headers = {
        "Title": s["title"],
        "Priority": PRIORITY.get(s["kind"], "default"),
        "Tags": TAGS.get(s["kind"], "chart_with_upwards_trend"),
    }
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    body = s["detail"] + (f"\n\nRule: {s['rule']}" if s.get("rule") else "")
    if cfg["app_url"]:
        link = f"{cfg['app_url']}/trader"
        headers["Click"] = link
        headers["Actions"] = f"view, Open in app, {link}"
    import requests  # here, so the offline self-test needs no network stack

    try:
        r = requests.post(f"{cfg['url']}/{cfg['topic']}", data=body.encode("utf-8"), headers=headers, timeout=timeout)
        return 200 <= r.status_code < 300
    except requests.RequestException:
        return False


def push_text(title: str, body: str, cfg: dict | None = None, priority: str = "default") -> bool:
    return push({"title": title, "detail": body, "kind": "note" if priority == "low" else "csp"}, cfg)
