"""
The Grant Map – Automated Scraper
===================================
Scrapes ICMR and ANRF grant deadlines daily.
Merges with static_grants.json (manually maintained).
Outputs grants_live.json for the dashboard.

Run locally:    python scraper.py
Run on server:  GitHub Actions (see .github/workflows/scrape.yml)

Requirements:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
"""

import json
import re
import logging
import traceback
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")
NOW_IST = datetime.now(IST)
TODAY = NOW_IST.date()
SCRAPED_AT = NOW_IST.strftime("%Y-%m-%dT%H:%M:%S+05:30")
SCRAPED_LABEL = NOW_IST.strftime("%d %b %Y, %I:%M %p IST")

OUTPUT_FILE = Path("grants_live.json")
STATIC_FILE = Path("static_grants.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("grant_map")


# ──────────────────────────────────────────────
# STATUS COMPUTATION
# ──────────────────────────────────────────────
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(text: str) -> date | None:
    """Parse a date string into a date object. Returns None if unparseable."""
    if not text:
        return None
    text = text.strip().lower()

    # ISO format: 2026-05-08
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # "30 april 2026" / "april 30, 2026" / "30 apr 2026"
    m = re.search(
        r"(\d{1,2})\s+([a-z]+)\s+(\d{4})|([a-z]+)\s+(\d{1,2})[,\s]+(\d{4})", text
    )
    if m:
        if m.group(1):
            day, mon, year = int(m.group(1)), m.group(2), int(m.group(3))
        else:
            mon, day, year = m.group(4), int(m.group(5)), int(m.group(6))
        month_num = MONTH_MAP.get(mon[:3])
        if month_num:
            return date(year, month_num, day)

    return None


def compute_status(close_date: date | None, open_date: date | None = None) -> str:
    """
    Compute grant status based on dates vs today.
    open_date < TODAY < close_date → open or closing
    TODAY < open_date             → upcoming
    TODAY > close_date            → closed
    close_date is None            → rolling
    """
    if close_date is None:
        return "rolling"
    if TODAY > close_date:
        return "closed"
    if open_date and TODAY < open_date:
        return "upcoming"
    days_left = (close_date - TODAY).days
    if days_left <= 30:
        return "closing"
    return "open"


def days_remaining(close_date: date | None) -> int | None:
    if close_date is None:
        return None
    return (close_date - TODAY).days


def fmt_dl_label(d: date | None) -> str:
    if d is None:
        return "No closing date (rolling)"
    return d.strftime("%b %-d, %Y")


# ──────────────────────────────────────────────
# SCRAPER A — ICMR
# ──────────────────────────────────────────────
def scrape_icmr() -> list[dict]:
    """
    Scrape https://www.icmr.gov.in/call-for-proposals
    Returns list of grant dicts with computed status.
    """
    url = "https://www.icmr.gov.in/call-for-proposals"
    log.info(f"Scraping ICMR: {url}")
    grants = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # ICMR lists calls as <li> or <a> items; text contains "Last Date:"
        items = soup.find_all(
            lambda tag: tag.name in ("li", "a", "p", "div")
            and "last date" in tag.get_text(separator=" ").lower()
        )

        seen_titles = set()
        for item in items:
            raw = item.get_text(separator=" ", strip=True)

            # Extract deadline
            dl_match = re.search(
                r"last\s+date[:\s]+([A-Za-z0-9 ,]+\d{4})", raw, re.IGNORECASE
            )
            if not dl_match:
                continue
            dl_text = dl_match.group(1).strip()
            close_d = parse_date(dl_text)

            # Extract title (text before "Last Date")
            title_raw = re.split(r"last\s+date", raw, flags=re.IGNORECASE)[0].strip()
            title_raw = re.sub(r"\s+", " ", title_raw).strip(" .()")
            if not title_raw or len(title_raw) < 10:
                continue

            # Deduplicate
            key = title_raw[:60].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            # Find apply URL
            link = item.find("a", href=True) if item.name != "a" else item
            apply_url = (
                link["href"] if link and link.get("href") else url
            )
            if apply_url.startswith("/"):
                apply_url = "https://www.icmr.gov.in" + apply_url

            status = compute_status(close_d)
            dr = days_remaining(close_d)

            grants.append({
                "id": f"icmr_scraped_{len(grants)+1}",
                "funder": "ICMR",
                "cat": "🔄 Live – ICMR",
                "title": title_raw,
                "status": status,
                "dl": close_d.isoformat() if close_d else None,
                "dlLabel": (
                    f"{fmt_dl_label(close_d)}"
                    + (" 🚨 URGENT" if dr is not None and dr <= 2 else "")
                ),
                "days_remaining": dr,
                "funding": "As per ICMR norms",
                "cat2": "ICMR Grant / EoI",
                "tags": ["India", "Clinical", "Health", "Pharmacy"],
                "focus": "Health research — see official ICMR page for full details.",
                "url": apply_url,
                "source": f"icmr.gov.in (auto-scraped {SCRAPED_LABEL})",
                "elig": [
                    "Indian researchers at recognised institutions",
                    "PI must hold MD/PhD or equivalent",
                    "See official call PDF for full eligibility",
                ],
                "inelig": [],
                "scraped": True,
                "scraped_at": SCRAPED_AT,
            })

        log.info(f"  ICMR: found {len(grants)} calls")

    except Exception as e:
        log.error(f"  ICMR scrape failed: {e}")
        log.debug(traceback.format_exc())

    return grants


# ──────────────────────────────────────────────
# SCRAPER B — ANRF via phdtalks.org
# ──────────────────────────────────────────────
ANRF_SCHEDULE_URL = "https://phdtalks.org/2026/01/anrf-upcoming-calls-2026.html"

# Fallback hardcoded schedule (verified Apr 28 2026)
ANRF_FALLBACK = [
    {"name": "JC Bose Grant",            "open": "2026-01-01", "close": "2026-02-10"},
    {"name": "National Post-Doctoral Fellowship (NPDF)", "open": "2026-01-15", "close": "2026-02-17"},
    {"name": "Advanced Research Grant (ARG) – Pre-Proposals", "open": "2026-04-01", "close": "2026-05-08"},
    {"name": "Advanced Research Grant (ARG) – MATRICS",       "open": "2026-04-01", "close": "2026-05-08"},
    {"name": "National Science Chair (NSC)",                   "open": "2026-07-01", "close": "2026-07-31"},
    {"name": "Advanced Research Grant (ARG) – Full Proposals", "open": "2026-09-15", "close": "2026-10-15"},
    {"name": "Inclusivity Research Grant (IRG)",               "open": "2026-10-01", "close": "2026-11-03"},
    {"name": "PM Early Career Research Grant (PMECRG)",        "open": "2026-11-02", "close": "2026-12-02"},
    {"name": "Ramanujan Fellowship",                           "open": None,         "close": None},
]

ANRF_TAGS = {
    "npdf":          ["India", "Fellowship", "Health", "Clinical", "Pharmacy"],
    "jc bose":       ["India", "Fellowship", "Health"],
    "ramanujan":     ["India", "Fellowship", "Health", "Clinical", "Pharmacy"],
    "arg":           ["India", "Clinical", "Health", "Pharmacy"],
    "matrics":       ["India", "Clinical", "Health", "Pharmacy"],
    "nsc":           ["India", "Health", "Fellowship"],
    "irg":           ["India", "Health", "Clinical", "Public Health"],
    "pmecrg":        ["India", "Fellowship", "Health", "Clinical", "Pharmacy"],
}

ANRF_ELIG = {
    "default": [
        "Indian researchers at UGC/AICTE-recognised institutions",
        "PhD qualification mandatory for PI",
        "Permanent or regular faculty position required",
        "Pharmacy, clinical research, biomedical science all eligible",
    ],
    "npdf": [
        "Indian PhD holders within 2 years of degree award",
        "Age ≤35 years (relaxation for SC/ST/OBC/Women)",
        "All disciplines including pharmacy and clinical research",
    ],
    "jc bose": [
        "Very senior Indian scientists with outstanding contributions",
        "Must hold regular position at recognised Indian institution",
    ],
    "ramanujan": [
        "Indian researchers currently working outside India",
        "Age ≤40 years at time of application",
        "Must commit to working in India full-time for 5 years",
    ],
    "nsc": [
        "Eminent senior Indian scientists with international recognition",
        "All disciplines including biomedical, pharmacy, clinical research",
    ],
}


def _anrf_tags(name: str) -> list[str]:
    nl = name.lower()
    for k, v in ANRF_TAGS.items():
        if k in nl:
            return v
    return ["India", "Health", "Clinical", "Pharmacy"]


def _anrf_elig(name: str) -> list[str]:
    nl = name.lower()
    for k, v in ANRF_ELIG.items():
        if k != "default" and k in nl:
            return v
    return ANRF_ELIG["default"]


def _build_anrf_grant(name: str, open_str: str | None, close_str: str | None, idx: int) -> dict:
    open_d  = parse_date(open_str)  if open_str  else None
    close_d = parse_date(close_str) if close_str else None
    status  = compute_status(close_d, open_d)
    dr      = days_remaining(close_d)

    dl_label = fmt_dl_label(close_d)
    if status == "rolling":
        dl_label = "Rolling (decisions twice yearly)"
    if dr is not None and dr <= 2:
        dl_label += " 🚨 URGENT"

    return {
        "id": f"anrf_scraped_{idx}",
        "funder": "ANRF (India)",
        "cat": "🔄 Live – ANRF 2026",
        "title": name,
        "status": status,
        "dl": close_d.isoformat() if close_d else None,
        "dlLabel": dl_label,
        "days_remaining": dr,
        "funding": "Amount as per ANRF norms (multi-year)",
        "cat2": (
            "Post-Doctoral Fellowship" if "npdf" in name.lower()
            else "Senior Fellowship"   if any(x in name.lower() for x in ["jc bose", "nsc", "ramanujan"])
            else "Early Career Grant"  if "pmecrg" in name.lower()
            else "Inclusivity Grant"   if "irg" in name.lower()
            else "Research Grant"
        ),
        "tags": _anrf_tags(name),
        "focus": (
            "Advanced research across all disciplines including pharmacy, "
            "clinical research, pharmacovigilance, biomedical, and neuroscience. "
            "Check anrfonline.in for full call details."
        ),
        "url": "https://anrfonline.in",
        "source": f"anrfonline.in / phdtalks.org (verified {SCRAPED_LABEL})",
        "elig": _anrf_elig(name),
        "inelig": [
            "Contract or temporary employees without permanent position",
            "Private college faculty without institutional recognition",
        ],
        "scraped": True,
        "scraped_at": SCRAPED_AT,
    }


def scrape_anrf() -> list[dict]:
    """
    Scrapes ANRF 2026 schedule from phdtalks.org HTML table.
    Falls back to hardcoded verified schedule if scrape fails.
    """
    log.info(f"Scraping ANRF schedule: {ANRF_SCHEDULE_URL}")
    rows = []

    try:
        resp = requests.get(ANRF_SCHEDULE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.find("table")
        if not table:
            raise ValueError("No table found on phdtalks ANRF page")

        for tr in table.find_all("tr")[1:]:  # skip header
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            name   = re.sub(r"^\d+\.\s*", "", cells[1]).strip()
            open_s = cells[2].strip()
            close_s = cells[3].strip() if len(cells) > 3 else ""

            # Ramanujan row has merged cell "Open throughout the year"
            if "throughout" in open_s.lower() or "throughout" in close_s.lower():
                open_s, close_s = None, None

            rows.append({"name": name, "open": open_s, "close": close_s})

        log.info(f"  ANRF: scraped {len(rows)} rows from phdtalks")

    except Exception as e:
        log.warning(f"  ANRF phdtalks scrape failed ({e}) — using fallback schedule")
        rows = [
            {"name": r["name"], "open": r["open"], "close": r["close"]}
            for r in ANRF_FALLBACK
        ]

    grants = []
    for i, r in enumerate(rows):
        grants.append(_build_anrf_grant(r["name"], r.get("open"), r.get("close"), i + 1))

    return grants


# ──────────────────────────────────────────────
# SCRAPER C — ANRF Direct (Playwright, optional)
# Used as a secondary check when phdtalks is stale
# ──────────────────────────────────────────────
def scrape_anrf_playwright() -> list[str]:
    """
    Directly scrapes anrfonline.in using Playwright (Angular SPA).
    Returns raw deadline strings found on the page.
    Only called if phdtalks data seems outdated.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — skipping direct ANRF scrape")
        return []

    log.info("Scraping ANRF direct via Playwright...")
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://anrfonline.in", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)  # extra wait for Angular rendering
            content = page.content()
            browser.close()

        # Extract date-like strings near grant keywords
        matches = re.findall(
            r"((?:ARG|MATRICS|NPDF|NSC|IRG|PMECRG|Ramanujan|Bose)"
            r".{0,80}?"
            r"(?:\d{1,2}[\s\-/]\w+[\s\-/]\d{4}|\d{4}-\d{2}-\d{2}))",
            content,
            re.IGNORECASE | re.DOTALL,
        )
        results = [m.strip() for m in matches]
        log.info(f"  ANRF direct: found {len(results)} date mentions")
    except Exception as e:
        log.error(f"  ANRF Playwright scrape failed: {e}")

    return results


# ──────────────────────────────────────────────
# LOAD STATIC GRANTS
# ──────────────────────────────────────────────
def load_static_grants() -> list[dict]:
    """
    Load manually maintained grants from static_grants.json.
    These are grants that cannot be scraped (MRC, Merck, Fogarty, etc.)
    Status is recomputed from dates on each run so it auto-updates.
    """
    if not STATIC_FILE.exists():
        log.warning(f"  {STATIC_FILE} not found — no static grants loaded")
        return []

    with open(STATIC_FILE, encoding="utf-8") as f:
        data = json.load(f)

    grants = []
    for g in data.get("grants", []):
        close_d = parse_date(g.get("dl")) if g.get("dl") else None
        open_d  = parse_date(g.get("open_date")) if g.get("open_date") else None

        if g.get("rolling"):
            g["status"] = "rolling"
            g["days_remaining"] = None
        elif close_d:
            g["status"] = compute_status(close_d, open_d)
            g["days_remaining"] = days_remaining(close_d)
            dr = g["days_remaining"]
            g["dlLabel"] = fmt_dl_label(close_d) + (
                " 🚨 URGENT" if dr is not None and dr <= 2 else ""
            )
        else:
            # No deadline and not rolling → anticipated (monitor funder site)
            g["status"] = g.get("status", "anticipated")
            g["days_remaining"] = None

        grants.append(g)

    log.info(f"  Static grants loaded: {len(grants)}")
    return grants


# ──────────────────────────────────────────────
# MERGE & OUTPUT
# ──────────────────────────────────────────────
def run_full_scraper():
    log.info("=" * 55)
    log.info("The Grant Map – Daily Scraper")
    log.info(f"Run date: {SCRAPED_LABEL}")
    log.info("=" * 55)

    # 1. Scrape live sources
    icmr_grants  = scrape_icmr()
    anrf_grants  = scrape_anrf()
    static_grants = load_static_grants()

    # 2. Merge: scraped first (they're most current), then static
    all_grants = anrf_grants + icmr_grants + static_grants

    # 3. Compute summary stats
    by_status = {}
    for g in all_grants:
        s = g.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

    urgent = [
        g["title"] for g in all_grants
        if g.get("days_remaining") is not None and 0 <= g["days_remaining"] <= 7
    ]

    output = {
        "scraped_at":    SCRAPED_AT,
        "scraped_label": SCRAPED_LABEL,
        "total":         len(all_grants),
        "by_status":     by_status,
        "urgent_grants": urgent,
        "grants":        all_grants,
    }

    # 4. Write output
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    log.info(f"\n{'='*55}")
    log.info(f"Output written to: {OUTPUT_FILE}")
    log.info(f"Total grants:      {len(all_grants)}")
    for s, n in sorted(by_status.items()):
        log.info(f"  {s:<12}: {n}")
    if urgent:
        log.info(f"\n⚠ URGENT (≤7 days): {len(urgent)} grant(s)")
        for u in urgent:
            log.info(f"  • {u}")
    log.info("=" * 55)

    return output


# ──────────────────────────────────────────────
# ALERT SYSTEM (optional)
# ──────────────────────────────────────────────
def send_email_alert(urgent_grants: list[str], smtp_config: dict):
    """
    Send email alert for urgent grants.
    smtp_config = {
        "host": "smtp.gmail.com", "port": 587,
        "user": "you@gmail.com", "password": "app_password",
        "to": ["recipient@gmail.com"]
    }
    Set as GitHub Actions secrets: SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL
    """
    import smtplib
    from email.mime.text import MIMEText

    if not urgent_grants:
        return

    body = "⚠ The Grant Map – Urgent Deadline Alert\n\n"
    body += f"The following grants close within 7 days (as of {SCRAPED_LABEL}):\n\n"
    for g in urgent_grants:
        body += f"  • {g}\n"
    body += "\nVisit The Grant Map dashboard to view full details and apply.\n"

    msg = MIMEText(body)
    msg["Subject"] = f"⚠ Grant Map Alert – {len(urgent_grants)} deadline(s) closing soon"
    msg["From"]    = smtp_config["user"]
    msg["To"]      = ", ".join(smtp_config["to"])

    try:
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as s:
            s.starttls()
            s.login(smtp_config["user"], smtp_config["password"])
            s.sendmail(smtp_config["user"], smtp_config["to"], msg.as_string())
        log.info(f"Email alert sent to {smtp_config['to']}")
    except Exception as e:
        log.error(f"Email alert failed: {e}")


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    result = run_full_scraper()

    # Optional: email alerts
    # Uncomment and configure if you want email alerts
    # import os
    # if result["urgent_grants"]:
    #     send_email_alert(result["urgent_grants"], {
    #         "host":     "smtp.gmail.com",
    #         "port":     587,
    #         "user":     os.environ["SMTP_USER"],
    #         "password": os.environ["SMTP_PASSWORD"],
    #         "to":       [os.environ["ALERT_EMAIL"]],
    #     })
