"""Watch the Shimanami rental-cycle stock API and alert on Telegram.

The public booking site (https://www.shimanami-bike-rental.com/booking/term)
is a Nuxt SPA that reads its availability from a plain JSON endpoint:

    GET https://shimanami.sports.navitime.jp/shimanami/bookings/stocks
        ?start=YYYY-MM-DD&end=YYYY-MM-DD

We query that endpoint directly, so no browser and no dependencies are needed.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

STOCK_API = "https://shimanami.sports.navitime.jp/shimanami/bookings/stocks"
BOOKING_URL = "https://www.shimanami-bike-rental.com/booking/term"

# Comma-separated list, so extra fallback dates can be added without code changes.
TARGET_DATES = [d.strip() for d in os.getenv("TARGET_DATES", "2026-10-15").split(",") if d.strip()]

# Terminal ①尾道駅前レンタサイクル (Onomichi Station).
PORT_ID = os.getenv("PORT_ID", "806821")
PORT_LABEL = os.getenv("PORT_LABEL", "Onomichi Station (尾道駅前)")

# 電動アシスト自転車 = Battery-Assisted Bicycle.
# Matched exactly: チャイルドシート付電動アシスト自転車 (child-seat model) is a
# different product and must NOT count as a hit.
CYCLE_TYPE = os.getenv("CYCLE_TYPE", "電動アシスト自転車")
CYCLE_LABEL = os.getenv("CYCLE_LABEL", "Battery-Assisted Bicycle")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
WORKFLOW_FILENAME = os.getenv("WORKFLOW_FILENAME", "shimanami-watch.yml")

DEBUG_DIR = Path("debug")


def http_json(url: str, method: str = "GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "shimanami-bike-watch/2.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, (json.loads(body) if body.strip() else None)


def telegram_send(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    http_json(url, "POST", {"chat_id": TELEGRAM_CHAT_ID, "text": message})


def disable_this_workflow() -> None:
    """Stop the schedule after the first successful alert."""
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        print("GitHub metadata unavailable; workflow will not auto-disable.")
        return
    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/"
        f"workflows/{WORKFLOW_FILENAME}/disable"
    )
    try:
        status, _ = http_json(
            url,
            "PUT",
            payload=None,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        print(f"Workflow disable returned {status}.")
    except urllib.error.HTTPError as exc:
        print(f"Warning: could not disable workflow: {exc.code} {exc.read()[:300]!r}")


def fetch_stocks(start: str, end: str):
    _, data = http_json(f"{STOCK_API}?start={start}&end={end}")
    return data


def iso(date_field) -> str:
    year, month, day = date_field
    return f"{year:04d}-{month:02d}-{day:02d}"


def count_for(day_entry) -> int:
    """Units of the wanted bike at the wanted terminal, or -1 if not offered."""
    found = -1
    for item in day_entry.get("availables", []):
        if str(item.get("port", {}).get("id")) != PORT_ID:
            continue
        if item.get("cycle", {}).get("type") != CYCLE_TYPE:
            continue
        found = int(item.get("availableCount", 0))
    return found


def main() -> int:
    if "--ping" in sys.argv:
        telegram_send(
            "✅ Shimanami watcher: тестовое сообщение. "
            "Связка бота и chat_id работает."
        )
        print("Ping sent.")
        return 0

    start, end = min(TARGET_DATES), max(TARGET_DATES)
    data = fetch_stocks(start, end)

    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "stocks.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    by_date = {iso(entry["date"]): entry for entry in data}
    hits = []

    for target in TARGET_DATES:
        entry = by_date.get(target)
        if entry is None:
            raise RuntimeError(
                f"The stock API returned no data for {target}. Either the booking "
                "window has not opened yet, or the API contract changed. "
                "See the shimanami-debug artifact."
            )
        count = count_for(entry)
        if count < 0:
            raise RuntimeError(
                f'No "{CYCLE_TYPE}" offered at port {PORT_ID} on {target}. '
                "Port or cycle identifiers probably changed; check debug/stocks.json."
            )
        print(f"{target}: {CYCLE_LABEL} @ {PORT_LABEL} -> availableCount={count}")
        if count > 0:
            hits.append((target, count))

    if not hits:
        print("Still sold out. No Telegram message sent.")
        return 0

    lines = [
        "\U0001f6b2 SHIMANAMI: появился Battery-Assisted Bicycle!",
        "",
        f"\U0001f4cd Получение: {PORT_LABEL}",
        f"\U0001f6b4 Велосипед: {CYCLE_LABEL}",
        "",
    ]
    for target, count in hits:
        lines.append(f"\U0001f4c5 {target} — свободно: {count} шт.")
    lines += ["", "Бронируй сейчас:", BOOKING_URL]

    telegram_send("\n".join(lines))
    print("Telegram notification sent.")
    disable_this_workflow()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Failures are deliberately not sent to Telegram: a transient API error
        # must not spam the user. GitHub emails you about the failed run instead.
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
