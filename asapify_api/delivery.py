from __future__ import annotations

import os

import httpx

from .core import ApiProblem


def prepare_delivery(repo, cluster: dict) -> tuple[str, str, str]:
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() != "true":
        raise ApiProblem(503, "DELIVERY_NOT_CONFIGURED", "Pengiriman Telegram belum diaktifkan.")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ApiProblem(503, "DELIVERY_NOT_CONFIGURED", "Token bot belum dikonfigurasi.")
    images = (cluster.get("verification") or {}).get("images", [])
    # Only pipeline-produced URLs are used; request bodies cannot select the destination/photo.
    preferred = sorted(images, key=lambda i: i.get("source") != "himawari_image")
    if not preferred or not preferred[0].get("url", "").startswith("https://"):
        raise ApiProblem(503, "DELIVERY_NOT_READY", "Citra bukti belum tersedia untuk peringatan.")
    return token, repo.telegram_chat(cluster["province"]), preferred[0]["url"]


def send_photo(prepared: tuple[str, str, str], caption: str) -> dict:
    token, chat, image = prepared
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/sendPhoto", json={
                "chat_id": chat, "photo": image, "caption": caption, "parse_mode": "HTML"})
        data = response.json()
        if response.is_success and data.get("ok"):
            return {"status": "sent", "message_id": data["result"]["message_id"], "error": None}
        return {"status": "failed", "message_id": None, "error": "TELEGRAM_REJECTED"}
    except Exception:
        # A timeout can happen AFTER Telegram accepted the message. Never automatically retry.
        return {"status": "unknown", "message_id": None, "error": "DELIVERY_UNCERTAIN"}
