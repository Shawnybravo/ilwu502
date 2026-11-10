"""One-time, secret-safe setup for Telegram's existing webhook.

This reads the current webhook URL from Telegram and re-registers the SAME URL
with the requested verification header. No bot token or secret is printed.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def telegram_api(token, method, data=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = urllib.parse.urlencode(data, doseq=True).encode() if data else None
    request = urllib.request.Request(url, data=encoded, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.URLError as error:
        # An HTTPError includes the token-bearing URL in its repr; never log it.
        raise RuntimeError(f"Telegram {method} request failed ({type(error).__name__})") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} returned an unsuccessful response")
    return result["result"]


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    secret = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
    if not token or not secret:
        raise RuntimeError("Missing Telegram bot token or webhook secret")
    if len(secret) > 256 or not all(char.isalnum() and char.isascii() or char in "_-" for char in secret):
        raise RuntimeError("Webhook secret must contain only ASCII letters, numbers, _ or -")

    info = telegram_api(token, "getWebhookInfo")
    url = info.get("url", "")
    if not url.startswith("https://"):
        raise RuntimeError("No HTTPS webhook currently configured; refusing to invent a URL")

    parameters = {
        "url": url,
        "secret_token": secret,
        "drop_pending_updates": "false",
    }
    if info.get("allowed_updates"):
        parameters["allowed_updates"] = json.dumps(info["allowed_updates"])
    if info.get("max_connections"):
        parameters["max_connections"] = str(info["max_connections"])

    telegram_api(token, "setWebhook", parameters)
    updated = telegram_api(token, "getWebhookInfo")
    if updated.get("url") != url:
        raise RuntimeError("Webhook URL changed unexpectedly")
    print("Telegram webhook verification is configured; existing URL preserved.")


if __name__ == "__main__":
    main()
