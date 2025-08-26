#!/usr/bin/env python3
"""
Epic Games "Free Now" checker
- Fetches current free promos from Epic's public JSON endpoints
- De-dupes using a local state file
- Sends a polished HTML email to one or more recipients
- Designed for GitHub Actions (env-only secrets) or local/Unraid use

Requires: requests
"""

import os
import sys
import json
import time
import html
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
import requests

# ----------------- Config via env (no secrets in code) -----------------
LOCALE   = os.getenv("EPIC_LOCALE", "en-US")
COUNTRY  = os.getenv("EPIC_COUNTRY", "US")
TZNAME   = os.getenv("TIMEZONE", "America/New_York")  # your local tz
STATE    = os.getenv("STATE_FILE", "state.json")

# SMTP (all via env). Auth is optional; only used if both user & pass present.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM") or SMTP_USER or "epic-free-checker@localhost"

# Recipients: comma-separated in EMAIL_TO_CSV
EMAIL_TO_CSV = os.getenv("EMAIL_TO_CSV", "")
EMAIL_TO = [e.strip() for e in EMAIL_TO_CSV.split(",") if e.strip()]

# Optional guard to achieve exact spacing when you run on a schedule (e.g., 6)
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "0") or "0")

# Endpoint(s) Epic commonly exposes for free promos:
EPIC_JSON_ENDPOINTS = [
    f"https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale={LOCALE}&country={COUNTRY}&allowCountries={COUNTRY}",
    f"https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions?locale={LOCALE}&country={COUNTRY}&allowCountries={COUNTRY}",
]

STORE_BASE = "https://store.epicgames.com"
LOCAL_TZ = ZoneInfo(TZNAME)


# --------------- Helpers --------------------------
def load_state(path: str) -> dict:
    """Load state: { "notified": {key: true, ...}, "last_success_iso": str|None }"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"notified": {}, "last_success_iso": None}


def save_state(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def http_get_json(url: str, timeout=20, attempts=3, backoff=2.0):
    """GET JSON with simple retries and a friendly User-Agent."""
    last = None
    for i in range(attempts):
        try:
            r = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "EpicFreeChecker/1.0 (+https://github.com/your-username/your-repo)",
                },
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
    raise last


def pick_best_image(key_images: list) -> str | None:
    """Choose the best-looking image by type priority."""
    if not key_images:
        return None
    prio = [
        "OfferImageTall",
        "DieselStoreFrontTall",
        "VaultClosed",
        "DieselStoreFrontWide",
        "OfferImageWide",
        "Thumbnail",
    ]
    by_type = {img.get("type"): img.get("url") for img in key_images if img.get("url")}
    for t in prio:
        if t in by_type:
            return by_type[t]
    return key_images[0].get("url")


def build_product_url(item: dict) -> str:
    """Construct a stable product URL from the catalog item."""
    slug = None
    try:
        mappings = item.get("catalogNs", {}).get("mappings") or []
        for m in mappings:
            if m.get("pageSlug"):
                slug = m["pageSlug"]
                break
    except Exception:
        pass
    if not slug:
        slug = item.get("productSlug") or item.get("urlSlug")
        if slug and not slug.startswith("p/"):
            slug = f"p/{slug}"
    if slug:
        # ensure we don't end up with malformed https:/ or double slashes
        return f"{STORE_BASE}/{LOCALE}/{slug}".replace("//", "/").replace("https:/", "https://")
    return f"{STORE_BASE}/{LOCALE}/search?q={requests.utils.quote(item.get('title',''))}"


def parse_free_now_items(payload: dict, tz: ZoneInfo):
    """Extract 'FREE NOW' promos from Epic response."""
    elements = payload.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    now_utc = datetime.now(timezone.utc)
    result = []

    for itm in elements:
        promos = itm.get("promotions") or {}
        current = promos.get("promotionalOffers") or []
        if not current:
            continue
        try:
            offers = current[0].get("promotionalOffers") or []
        except Exception:
            offers = []
        if not offers:
            continue

        offer = offers[0]
        end_iso = offer.get("endDate")
        start_iso = offer.get("startDate")
        ds = offer.get("discountSetting") or {}
        is_free = (ds.get("discountType") == "PERCENTAGE" and int(ds.get("discountPercentage", 0)) == 0)

        if not end_iso:
            continue
        try:
            end_dt_utc = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except Exception:
            continue

        # Must be in the future
        if end_dt_utc <= now_utc:
            continue

        # If not explicitly 0% free yet, ensure we're within the promo window
        if not is_free and start_iso:
            try:
                start_dt_utc = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                if not (start_dt_utc <= now_utc <= end_dt_utc):
                    continue
            except Exception:
                continue

        title = (itm.get("title") or "").strip()
        img = pick_best_image(itm.get("keyImages") or [])
        url = build_product_url(itm)
        ends_local = end_dt_utc.astimezone(tz)

        result.append({
            "title": title,
            "image": img,
            "url": url,
            "ends_at_utc": end_dt_utc.isoformat(),
            "ends_at_local": ends_local.isoformat(),
        })

    # Soonest-ending first
    result.sort(key=lambda x: x["ends_at_utc"])
    return result


def fetch_free_now(tz: ZoneInfo):
    """Try both endpoints before giving up."""
    last_exc = None
    for url in EPIC_JSON_ENDPOINTS:
        try:
            payload = http_get_json(url)
            return parse_free_now_items(payload, tz)
        except Exception as e:
            last_exc = e
            continue
    raise RuntimeError(f"All Epic endpoints failed. Last error: {last_exc}")


# --------------- Email rendering ------------------
def render_email_html(items, tzname: str, header_title: str | None = None):
    """
    Responsive 'Minimalist Dark Mode' HTML email with centered content.
    header_title: if provided, used for the main header text (e.g., "Epic Free Games: Game1, Game2")
    """
    tz = ZoneInfo(tzname)
    titles = [g["title"] for g in items]
    dynamic_title = header_title or (f"Epic Free Games: {', '.join(titles)}" if titles else "Epic Free Games")
    preheader = f"New 'FREE NOW' games: {', '.join(titles)}" if titles else "No new 'FREE NOW' games right now."

    def fmt_local(iso_str: str) -> str:
        dt = datetime.fromisoformat(iso_str)
        dt = dt.astimezone(tz)
        abbrev = dt.tzname() or tzname
        return f"{dt.strftime('%a, %b %d, %I:%M %p').lstrip('0')} {abbrev}"

    cards = []
    for g in items:
        title = html.escape(g["title"])
        url = html.escape(g["url"])
        img = html.escape(g["image"] or "")
        ends_str = f"Free until {fmt_local(g['ends_at_local'])}"

        img_block = ""
        if img:
            img_block = f"""
              <tr>
                <td align="center" style="padding:16px 16px 0 16px;">
                  <a href="{url}" target="_blank" style="text-decoration:none;">
                    <img src="{img}" width="568" alt="{title} cover art"
                         style="display:block; width:100%; max-width:100%; height:auto; border-radius:14px; border:0; outline:none; text-decoration:none;
                                box-shadow:0 0 0 2px #17202b, 0 12px 28px rgba(0, 200, 255, 0.2);" />
                  </a>
                </td>
              </tr>
            """

        cards.append(f"""
          <!-- GAME CARD -->
          <tr>
            <td class="p-24" align="center" style="padding:24px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                     style="background:#0b0f15; border:1px solid #1e2a38; border-radius:18px;">
                {img_block}
                <tr>
                  <td align="center" style="padding:16px 20px 0 20px; text-align:center;">
                    <div class="title" style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-weight:700; font-size:24px; line-height:30px; color:#eef4ff; text-align:center;">
                      {title}
                    </div>
                    <div class="meta" style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-size:16px; line-height:24px; color:#a7b1c2; margin-top:8px; text-align:center;">
                      {html.escape(ends_str)}
                    </div>
                  </td>
                </tr>
                <tr>
                  <td align="center" style="padding:18px 20px 24px 20px;">
                    <table role="presentation" class="btn" cellspacing="0" cellpadding="0" border="0" align="center">
                      <tr>
                        <td align="center" bgcolor="#00C2FF" style="border-radius:14px;">
                          <a href="{url}" target="_blank"
                             style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-size:20px; font-weight:800; line-height:20px;
                                    text-decoration:none; padding:18px 28px; display:inline-block; color:#0b0e13;">
                            VIEW GAME
                          </a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        """)

    if not cards:
        cards_html = """
          <tr>
            <td align="center" style="padding:24px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                     style="background:#0b0f15; border:1px solid #1e2a38; border-radius:18px;">
                <tr>
                  <td align="center" style="padding:24px;">
                    <div style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; color:#a7b1c2; font-size:15px; line-height:22px; text-align:center;">
                      No "FREE NOW" games at the moment.
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        """
    else:
        cards_html = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
    <meta name="x-apple-disable-message-reformatting">
    <title>{html.escape(dynamic_title)}</title>
    <style>
      @media screen and (max-width: 600px) {{
        .container {{ width: 100% !important; }}
        .p-24 {{ padding: 16px !important; }}
        .title {{ font-size: 22px !important; line-height: 28px !important; }}
        .meta {{ font-size: 14px !important; line-height: 20px !important; }}
        .btn a {{ padding: 18px 24px !important; font-size: 18px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0; padding:0; background:#0b0e13; color:#e6e6e6; -webkit-font-smoothing:antialiased;">
    <center role="article" aria-roledescription="email" lang="en" style="width:100%; background:#0b0e13;">
      <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">
        {html.escape(preheader)}
      </div>
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#0b0e13;">
        <tr>
          <td align="center" style="padding:24px;">
            <table role="presentation" class="container" cellspacing="0" cellpadding="0" border="0" width="600"
                   style="width:600px; max-width:600px; background:#0f131a; border-radius:20px; box-shadow:0 10px 30px rgba(0,0,0,0.45);">
              <tr>
                <td align="center" style="padding:28px 28px 8px 28px;">
                  <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/3/31/Epic_Games_logo.svg/250px-Epic_Games_logo.svg.png" width="36" height="36" alt="Epic"
                       style="display:block; border:0; outline:none; text-decoration:none;"/>
                  <div style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-weight:700; font-size:24px; line-height:30px; margin-top:8px; letter-spacing:0.2px; text-align:center;">
                    {html.escape(dynamic_title)}
                  </div>
                  <div style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; color:#a7b1c2; font-size:14px; line-height:22px; margin-top:6px; text-align:center;">
                    See the latest free Epic Games this week.
                  </div>
                </td>
              </tr>
              <tr><td style="height:12px;"></td></tr>
              {cards_html}
              <tr>
                <td align="center" style="padding:4px 24px 28px 24px;">
                  <div style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-size:12px; line-height:18px; color:#78869a; text-align:center;">
                    You're receiving this because you set up Epic Games - Free Game Checker.
                  </div>
                  <div style="font-family:Segoe UI, Roboto, Helvetica, Arial, sans-serif; font-size:12px; line-height:18px; color:#495468; margin-top:4px; text-align:center;">
                    Tip: Add to contacts to avoid spam.
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </center>
  </body>
</html>"""


# --------------- Email sending --------------------
def send_email(subject: str, html_body: str) -> bool:
    """Send an email via SMTP. TLS if available, login if creds provided."""
    if not (SMTP_HOST and SMTP_PORT and EMAIL_TO):
        print("Email not configured; skipping send.", file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)

    # Minimal text fallback
    fallback = "New 'FREE NOW' games on Epic. Open this email in an HTML client."
    msg.attach(MIMEText(fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except Exception:
            # Some servers may already be TLS or not support it
            pass
        if SMTP_USER and SMTP_PASS:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    return True


# --------------- Orchestrator ---------------------
def main() -> int:
    state = load_state(STATE)
    now_utc = datetime.now(timezone.utc)

    # spacing guard to avoid spamming when on a frequent cron
    if MIN_HOURS_BETWEEN_RUNS > 0 and state.get("last_success_iso"):
        try:
            last = datetime.fromisoformat(state["last_success_iso"])
            if now_utc - last < timedelta(hours=MIN_HOURS_BETWEEN_RUNS):
                print("Skip: MIN_HOURS_BETWEEN_RUNS guard.", file=sys.stderr)
                return 0
        except Exception:
            pass

    try:
        items = fetch_free_now(LOCAL_TZ)
    except Exception as e:
        print(f"Error fetching promos: {e}", file=sys.stderr)
        # Do not update last_success_iso on fetch failure
        return 1

    # de-dupe using title + ends_at_utc as key
    notified = state.get("notified", {})
    new_items = []
    for it in items:
        key = f"{it['title']}|{it['ends_at_utc']}"
        if notified.get(key):
            continue
        new_items.append(it)

    # render & send only if there is something new
    if new_items:
        # Build subject/header like: "Epic Free Games: Game1, Game2"
        titles = [it["title"] for it in new_items]
        subject = f"Epic Free Games: {', '.join(titles)}"

        html_body = render_email_html(new_items, TZNAME, header_title=subject)

        sent = False
        try:
            sent = send_email(subject, html_body)
        except Exception as e:
            print(f"Error sending email: {e}", file=sys.stderr)
            # We still mark success for the run itself if fetching succeeded,
            # but we don't mark items as notified to retry next run.
        if sent:
            # After successful send, mark all currently visible items as notified
            for it in items:
                key = f"{it['title']}|{it['ends_at_utc']}"
                notified[key] = True
    else:
        print("No new items to notify.", file=sys.stderr)

    # update state (run considered successful if we fetched successfully)
    state["notified"] = notified
    state["last_success_iso"] = now_utc.isoformat()
    try:
        save_state(STATE, state)
    except Exception as e:
        print(f"Warning: failed to save state: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
