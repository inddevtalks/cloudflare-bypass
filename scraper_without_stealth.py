#!/usr/bin/env python3
"""
scraper_without_stealth.py — the version that works.

Drives real Chrome through Playwright, runs a search, and scrapes each result
page. Read README.md first: the whole point of this repository is that this
file and scraper_with_stealth.py are near-identical, and the difference is the
entire lesson.

Design notes worth stealing even if you never scrape anything:

  1. No fingerprint patching. Real Chrome already reports correct values.
     Overwriting them with generic ones is itself a detection signal.
  2. Fail loudly. Cloudflare's interstitial returns HTTP 200, so "did the
     request succeed?" cannot tell you anything. We check for the challenge
     explicitly and refuse to write a silently-empty output file.
  3. Checkpoint as you go. A 90-product run that dies at product 70 should
     not lose the first 69.
  4. Structured data first. JSON-LD / __NEXT_DATA__ outlive the markup, so
     read those before falling back to selectors.

Tested against a Cloudflare-managed-challenge site. It is not a CAPTCHA solver
and does not try to be one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Signals that we are looking at an interstitial rather than real content.
# Cloudflare rotates the wording, so we match on structure and keywords
# instead of a single exact phrase.
CHALLENGE_TITLE_HINTS = (
    "just a moment",
    "attention required",
    "security verification",
    "checking your browser",
    "access denied",
    "verifying you are human",
)

CHALLENGE_BODY_HINTS = (
    # Managed challenge - the copy that actually ships today. Verified
    # against a real interstitial screenshot, not guessed.
    "performing security verification",
    "this page is displayed while the website verifies",
    "security service to protect against malicious bots",
    # Other Cloudflare products that block the same way.
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "verify you are human",
    "ddos protection by",
    # Present on essentially every Cloudflare interstitial.
    "ray id",
)

CHALLENGE_IFRAME_HINTS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform",
)

CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#cf-challenge-running",
    "#challenge-running",
    "iframe[src*='challenges.cloudflare.com']",
    "div.cf-turnstile",
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    url: str = "https://www.backmarket.com/en-us"
    term: str = "ipads"
    limit: int = 100
    output: str = "products.json"
    shots_dir: str = "challenge_screenshots"

    locale: str = "en-US"
    timezone: str = "America/New_York"

    headful: bool = True
    channel: str = "chrome"
    viewport_width: int = 1366
    viewport_height: int = 768

    profile: str | None = None
    delay_min: float = 0.8
    delay_max: float = 1.6
    nav_timeout_ms: int = 60_000
    scroll_ms: int = 5_000

    respect_robots: bool = True

    def search_xpath(self) -> str:
        """Override the site's search box locator."""
        return getattr(self, "_search_xpath", '//*[@aria-describedby="resultsListBoxAlert"]')

    def product_xpath(self) -> str:
        """Override the site's product link locator."""
        return getattr(self, "_product_xpath", '//*[@data-spec="product-info"]//a')


# --------------------------------------------------------------------------- #
# Challenge detection — the part that matters most
# --------------------------------------------------------------------------- #


@dataclass
class ChallengeReport:
    detected: bool = False
    status: int | None = None
    title: str = ""
    url: str = ""
    signals: list[str] = field(default_factory=list)
    screenshot: str | None = None

    @property
    def status_note(self) -> str:
        if self.status is None:
            return "unknown (navigation did not return a response object)"
        if self.status == 200:
            return "200 OK - the challenge is itself a successful response"
        return f"{self.status}"

    def explain(self) -> str:
        if not self.detected:
            return "No challenge detected."

        lines = [
            "",
            "=" * 68,
            "  CLOUDFLARE CHALLENGE DETECTED - nothing was scraped.",
            "=" * 68,
            f"  HTTP status : {self.status_note}",
            f"  Page title  : {self.title!r}",
            f"  Final URL   : {self.url}",
            f"  Signals     : {', '.join(self.signals)}",
        ]

        if self.screenshot:
            lines.append(f"  Screenshot  : {self.screenshot}")

        lines += [
            "",
            "  A 200 with an interstitial means 'the request worked' tells you",
            "  nothing. The scraper is being served the challenge INSTEAD of the",
            "  page. This is why an empty output file is a block, not a bug.",
            "",
            "  Things that commonly fix it, in order of how often they work:",
            "",
            "    1. Delete the Chrome profile directory and start clean. A",
            "       profile that accumulated challenged attempts keeps getting",
            "       challenged. This is the single most common fix.",
            "    2. Run headful, not headless. A real window is a strong signal.",
            "    3. Use channel='chrome' so you drive the installed browser, not",
            "       bundled Chromium.",
            "    4. Make locale and timezone agree with the site you are on.",
            "    5. Do NOT patch navigator.webdriver or install playwright-stealth.",
            "       Over-realising a fingerprint is itself a signal, and the",
            "       stealth shim is byte-identical in every copy in existence.",
            "",
            "  If you are on a flagged datacenter IP, none of the above will",
            "  help. The decision is made before the client is ever scored.",
            "=" * 68,
            "",
        ]

        return "\n".join(lines)


class StatusTracker:
    """
    Records the status of the most recent main-frame document response.

    Needed because a challenge often arrives after a form submit or a
    client-side redirect, where there is no navigation response object to
    inspect - but the HTTP status is still the single most useful clue, since
    it is usually 200.
    """

    def __init__(self) -> None:
        self.status: int | None = None

    def attach(self, page: Page) -> None:
        def on_response(response) -> None:
            try:
                if response.request.resource_type == "document":
                    self.status = response.status
            except Exception:
                pass

        try:
            page.on("response", on_response)
        except Exception:
            pass


async def detect_challenge(page: Page, response=None, tracker: "StatusTracker | None" = None) -> ChallengeReport:
    """
    Decide whether the current page is a bot-check rather than real content.

    Checks structure first (challenge DOM), then title and body text. Returns
    a report either way so the caller can decide how loudly to fail. Pass the
    navigation response or a StatusTracker to record the real HTTP status.
    """

    report = ChallengeReport()
    report.url = page.url

    if response is not None:
        try:
            report.status = response.status
        except Exception:
            report.status = None

    if report.status is None and tracker is not None:
        report.status = tracker.status

    try:
        report.title = await page.title()
    except Exception:
        report.title = ""

    # 1. Structural markers. Fast and unambiguous when present.
    for selector in CHALLENGE_SELECTORS:
        try:
            if await page.locator(selector).count() > 0:
                report.signals.append(f"dom:{selector}")
        except Exception:
            continue

    # 2. Cloudflare challenge iframes.
    try:
        frames = [frame.url for frame in page.frames if frame.url]
        for frame_url in frames:
            if any(hint in frame_url for hint in CHALLENGE_IFRAME_HINTS):
                report.signals.append(f"iframe:{frame_url}")
    except Exception:
        pass

    # 3. Title wording. Cloudflare uses a small, stable set of titles.
    lowered_title = report.title.lower()

    for hint in CHALLENGE_TITLE_HINTS:
        if hint in lowered_title:
            report.signals.append(f"title:{hint}")
            break

    # 4. Body wording.
    try:
        body = (await page.locator("body").inner_text(timeout=5_000)).lower()
    except Exception:
        body = ""

    for hint in CHALLENGE_BODY_HINTS:
        if hint in body:
            report.signals.append(f"body:{hint}")
            break

    try:
        response = await page.evaluate(
            """() => {
                const meta = document.querySelector('meta[name="cf-ray"]');
                return meta ? meta.getAttribute('content') : null;
            }"""
        )
        if response:
            report.signals.append(f"cf-ray:{response}")
    except Exception:
        pass

    report.detected = bool(report.signals)

    return report


async def capture_challenge(page: Page, shots_dir: str) -> str | None:
    """Screenshot the interstitial so there is evidence, not just a guess."""

    try:
        directory = Path(shots_dir)
        directory.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"challenge-{stamp}.png"

        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Politeness
# --------------------------------------------------------------------------- #


def robots_allows(url: str, user_agent: str) -> bool:
    """
    Best-effort robots.txt check. Returns True when we cannot reach robots.txt,
    so a network hiccup never silently blocks a legitimate run.
    """

    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        import urllib.request

        with urllib.request.urlopen(robots_url, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception:
        return True

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())

    return parser.can_fetch(user_agent, url)


# --------------------------------------------------------------------------- #
# Structured data — read this before touching the DOM
# --------------------------------------------------------------------------- #


async def read_json_ld(page: Page) -> dict[str, Any] | None:
    """
    Pull the first useful JSON-LD object off the page.

    Sites publish Product schema for rich results, and it survives redesigns
    that break selectors. Cheapest, most durable source when it exists.
    """

    try:
        blobs = await page.locator('script[type="application/ld+json"]').all_text_contents()
    except Exception:
        return None

    for blob in blobs:
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            graph = candidate.get("@graph")
            if isinstance(graph, list):
                candidates.extend([g for g in graph if isinstance(g, dict)])

            if "@type" in candidate:
                types = candidate["@type"]
                types = types if isinstance(types, list) else [types]
                if any("product" in str(t).lower() for t in types):
                    return candidate

    return None


async def read_next_data(page: Page) -> dict[str, Any] | None:
    """
    Pull __NEXT_DATA__ when the site is a Next.js app.

    This is usually a much richer and far more stable source than scraping
    rendered markup.
    """

    try:
        raw = await page.evaluate(
            """() => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? el.textContent : null;
            }"""
        )
        return json.loads(raw) if raw else None
    except Exception:
        return None


def flatten_json_ld(product: dict[str, Any]) -> dict[str, Any]:
    """Pull the handful of fields we actually care out of a Product schema."""

    def pick(*keys: str) -> Any:
        for key in keys:
            if product.get(key):
                return product[key]
        return None

    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}

    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    price = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency") or ""

    return {
        "name": product.get("name"),
        "price": f"{price} {currency}".strip() if price else None,
        "image": image,
        "brand": (
            product.get("brand", {}).get("name")
            if isinstance(product.get("brand"), dict)
            else product.get("brand")
        ),
        "sku": product.get("sku"),
        "availability": offers.get("availability"),
        "source": "json-ld",
    }


# --------------------------------------------------------------------------- #
# DOM fallbacks
# --------------------------------------------------------------------------- #


async def first_text(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            text = (await locator.inner_text()).strip()
            if text:
                return " ".join(text.split())
        except Exception:
            continue

    return None


async def first_attribute(page: Page, selectors: list[str], attribute: str) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            value = await locator.get_attribute(attribute)
            if value:
                return value.strip()
        except Exception:
            continue

    return None


async def extract_image(page: Page) -> str | None:
    for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]', "img"):
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue

            for attribute in ("content", "src", "srcset"):
                value = await locator.get_attribute(attribute)
                if not value:
                    continue
                if attribute == "srcset":
                    value = value.split(",")[0].strip().split(" ")[0]
                return urljoin(page.url, value)
        except Exception:
            continue

    return None


# --------------------------------------------------------------------------- #
# Scraping steps
# --------------------------------------------------------------------------- #


async def run_search(page: Page, term: str, search_xpath: str) -> bool:
    print(f"[+] Locating search field for {term!r}...")

    box = page.locator(search_xpath)

    try:
        await box.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        print("[!] Search field not visible.")
        return False

    await box.click()
    await box.fill("")
    await box.fill(term)
    await box.press("Enter")

    print(f"[+] Search submitted: {term}")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        print("[!] Search navigation timed out; continuing.")

    await page.wait_for_timeout(3_000)

    return True


async def scroll_to_load(page: Page, duration_ms: int) -> None:
    """
    Scroll for a fixed period to trigger lazy-loaded content, then return to
    the top so selectors resolve against the whole page.
    """

    print(f"[+] Scrolling for {duration_ms} ms...")

    start = await page.evaluate("Date.now()")

    while True:
        elapsed = await page.evaluate("(s) => Date.now() - s", start)
        if elapsed >= duration_ms:
            break

        await page.evaluate(
            """() => window.scrollBy({
                top: Math.floor(window.innerHeight * 0.75),
                behavior: "smooth"
            })"""
        )
        await asyncio.sleep(0.8)

    await page.evaluate("() => window.scrollTo({ top: 0, behavior: " + '"smooth"' + " })")
    await page.wait_for_timeout(1_000)

    print("[+] Scrolling done.")


async def collect_links(page: Page, product_xpath: str, limit: int) -> list[str]:
    print("[+] Collecting product links...")

    locator = page.locator(product_xpath)

    try:
        await locator.first.wait_for(state="attached", timeout=20_000)
    except PlaywrightTimeoutError:
        print("[!] No product links matched the XPath.")
        return []

    count = await locator.count()
    links: list[str] = []

    for index in range(count):
        try:
            href = await locator.nth(index).get_attribute("href")
            if not href:
                continue
            absolute = urljoin(page.url, href)
            if absolute.startswith("http"):
                links.append(absolute)
        except Exception:
            continue

    # dict.fromkeys dedupes while preserving order
    unique = list(dict.fromkeys(links))

    if limit:
        unique = unique[:limit]

    print(f"[+] {len(unique)} unique product links.")
    return unique


async def scrape_product(
    page: Page,
    url: str,
    index: int,
    total: int,
    config: Config,
    tracker: "StatusTracker | None" = None,
) -> dict[str, Any]:
    print(f"[{index}/{total}] {url}")

    response = None
    for attempt in range(1, 4):
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms
            )
            break
        except PlaywrightTimeoutError:
            print(f"[{index}/{total}] Timeout (attempt {attempt}/3)")
            if attempt == 3:
                return {"url": url, "error": "navigation timeout"}
        except Exception as error:
            print(f"[{index}/{total}] Error (attempt {attempt}/3): {error}")
            if attempt == 3:
                return {"url": url, "error": str(error)}

    # A challenge can appear on a product page too, not just the results page.
    report = await detect_challenge(page, response, tracker)
    if report.detected:
        report.screenshot = await capture_challenge(page, config.shots_dir)
        print(f"[{index}/{total}] Challenge intercepted on product page.")
        print(report.explain())
        return {"url": url, "error": "cloudflare challenge", "challenge": report.signals}

    try:
        await page.locator("body").wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1_200)

    product: dict[str, Any] = {"url": page.url}

    # 1. Structured data first.
    json_ld = await read_json_ld(page)
    if json_ld:
        product.update(flatten_json_ld(json_ld))
        print(f"[{index}/{total}] via JSON-LD: {product.get('name')}")
    else:
        product["source"] = "dom"

        product["name"] = await first_text(
            page, ["h1", '[data-testid="product-title"]', '[data-spec="product-title"]']
        )
        if not product["name"]:
            product["name"] = await first_attribute(page, ['meta[property="og:title"]'], "content")

        product["price"] = await first_text(
            page,
            [
                '[data-testid*="price"]',
                '[data-spec*="price"]',
                '[aria-label*="price" i]',
                '[class*="price" i]',
            ],
        )
        if not product["price"]:
            product["price"] = await first_attribute(
                page, ['meta[property="product:price:amount"]'], "content"
            )

        product["image"] = await extract_image(page)
        print(f"[{index}/{total}] via DOM: {product.get('name')}")

    product["scraped_at"] = datetime.now().isoformat(timespec="seconds")

    return product


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def save(products: list[dict[str, Any]], config: Config, status: str) -> None:
    payload = {
        "status": status,
        "search_term": config.term,
        "source": config.url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(products),
        "products": products,
    }

    Path(config.output).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"[+] Saved {len(products)} products to {config.output} (status: {status})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Scrape a Cloudflare-protected site with real Chrome.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--url", default=Config.url, help="Site to start from")
    parser.add_argument("--term", default=Config.term, help="Search term")
    parser.add_argument("--limit", type=int, default=Config.limit, help="Max products (0 = all)")
    parser.add_argument("--output", default=Config.output, help="Output JSON path")
    parser.add_argument("--shots-dir", default=Config.shots_dir, help="Where challenge screenshots go")

    parser.add_argument("--locale", default=Config.locale)
    parser.add_argument("--timezone", default=Config.timezone)

    parser.add_argument("--headless", action="store_true", help="Force headless (less likely to work)")
    parser.add_argument("--channel", default=Config.channel, help="'chrome' for installed Chrome")
    parser.add_argument("--profile", default=None, help="Persistent profile dir, e.g. .chrome-profile")

    parser.add_argument("--delay-min", type=float, default=Config.delay_min)
    parser.add_argument("--delay-max", type=float, default=Config.delay_max)
    parser.add_argument("--scroll-ms", type=int, default=Config.scroll_ms)
    parser.add_argument("--search-xpath", default=Config().search_xpath())
    parser.add_argument("--product-xpath", default=Config().product_xpath())

    parser.add_argument("--ignore-robots", action="store_true", help="Skip the robots.txt check")
    parser.add_argument("--keep-open", action="store_true", help="Leave the browser open at the end")

    args = parser.parse_args(argv)

    config = Config(
        url=args.url,
        term=args.term,
        limit=args.limit,
        output=args.output,
        shots_dir=args.shots_dir,
        locale=args.locale,
        timezone=args.timezone,
        headful=not args.headless,
        channel=args.channel,
        profile=args.profile,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        scroll_ms=args.scroll_ms,
        respect_robots=not args.ignore_robots,
    )

    config._search_xpath = args.search_xpath      # type: ignore[attr-defined]
    config._product_xpath = args.product_xpath    # type: ignore[attr-defined]

    return config


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


async def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)

    print("=" * 68)
    print("  Cloudflare-friendly scraper (no stealth, no fingerprint patching)")
    print("=" * 68)
    print(f"  url      : {config.url}")
    print(f"  term     : {config.term}")
    print(f"  limit    : {config.limit or 'unlimited'}")
    print(f"  locale   : {config.locale}   timezone: {config.timezone}")
    print(f"  headful  : {config.headful}")
    print(f"  profile  : {config.profile or '(ephemeral)'}")
    print("=" * 68)

    if config.respect_robots:
        allowed = robots_allows(config.url, USER_AGENT)
        print(f"[+] robots.txt: {'allowed' if allowed else 'DISALLOWED'}")

        if not allowed:
            print("[!] robots.txt disallows this path. Stopping.")
            print("    Use --ignore-robots only if you have permission to do so.")
            return 2

    products: list[dict[str, Any]] = []
    exit_code = 0

    async with async_playwright() as pw:
        print("[+] Launching Chrome...")

        if config.profile:
            # Persistent context is what lets cf_clearance survive between runs.
            # If a run starts getting challenged after it was not, delete this
            # directory before debugging anything else.
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=config.profile,
                channel=config.channel,
                headless=not config.headful,
                viewport={
                    "width": config.viewport_width,
                    "height": config.viewport_height,
                },
                locale=config.locale,
                timezone_id=config.timezone,
                user_agent=USER_AGENT,
            )
            browser = None
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
        else:
            browser = await pw.chromium.launch(
                channel=config.channel,
                headless=not config.headful,
            )
            context = await browser.new_context(
                viewport={
                    "width": config.viewport_width,
                    "height": config.viewport_height,
                },
                locale=config.locale,
                timezone_id=config.timezone,
                user_agent=USER_AGENT,
            )
            page = await context.new_page()

        tracker = StatusTracker()
        tracker.attach(page)

        print(f"[+] Opening {config.url}")

        try:
            landing = await page.goto(
                config.url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms
            )
        except PlaywrightTimeoutError:
            landing = None
            print("[!] Initial navigation timed out; continuing anyway.")

        # ---- Gate 1: is the landing page a challenge? -----------------------
        report = await detect_challenge(page, landing, tracker)

        if report.detected:
            report.screenshot = await capture_challenge(page, config.shots_dir)
            print(report.explain())
            print("[!] Refusing to write an empty results file.")
            print("    An empty file with no error is the worst possible outcome:")
            print("    it looks like a working scraper that found nothing.")
            save(products, config, status="blocked_by_cloudflare")
            await context.close()
            if browser:
                await browser.close()
            return 1

        print(f"[+] No challenge. Title: {await page.title()}")

        if not await run_search(page, config.term, config.search_xpath()):
            save(products, config, status="search_field_not_found")
            await context.close()
            if browser:
                await browser.close()
            return 1

        # ---- Gate 2: check again after the search navigates ----------------
        # A block very often only appears on the results page, not the landing
        # page, so this second check is the one that usually catches it.
        report = await detect_challenge(page, None, tracker)

        if report.detected:
            report.screenshot = await capture_challenge(page, config.shots_dir)
            print(report.explain())
            print("[!] Refusing to write an empty results file.")
            print("    An empty file with no error is the worst possible outcome:")
            print("    it looks like a working scraper that found nothing.")
            save(products, config, status="blocked_by_cloudflare")
            await context.close()
            if browser:
                await browser.close()
            return 1

        await scroll_to_load(page, config.scroll_ms)

        links = await collect_links(page, config.product_xpath(), config.limit)

        if not links:
            report = await detect_challenge(page, None, tracker)
            if report.detected:
                report.screenshot = await capture_challenge(page, config.shots_dir)
                print(report.explain())
                save(products, config, status="blocked_by_cloudflare")
                await context.close()
                if browser:
                    await browser.close()
                return 1

            print("[!] Search worked but no product links matched.")
            print(f"    XPath was: {config.product_xpath()}")
            print("    That is a selector problem, not a block - find the real")
            print("    selector in DevTools and pass --product-xpath.")
            save(products, config, status="no_links_matched")
            await context.close()
            if browser:
                await browser.close()
            return 3

        total = len(links)

        for index, url in enumerate(links, start=1):
            product = await scrape_product(page, url, index, total, config, tracker)
            products.append(product)

            # Checkpoint every 10 products so a crash does not lose the run.
            if index % 10 == 0:
                save(products, config, status="in_progress")

            await asyncio.sleep(random.uniform(config.delay_min, config.delay_max))

        save(products, config, status="complete")

        good = sum(1 for p in products if not p.get("error"))
        print(f"[+] Done. {good}/{len(products)} products scraped successfully.")

        if good == 0:
            exit_code = 1

        if config.keep_open:
            print("[+] --keep-open set. Press ENTER to close the browser.")
            await asyncio.to_thread(input)

        await context.close()
        if browser:
            await browser.close()

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        sys.exit(130)