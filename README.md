# Scraping a Cloudflare-protected site without tripping the bot check

Two Playwright scrapers for the same target. One gets blocked by Cloudflare on
every single run and returns nothing. The other works. They differ by **four
lines**, and those four lines are the entire lesson.

| File | Outcome |
| --- | --- |
| [`scraper_with_stealth.py`](scraper_with_stealth.py) | Blocked. Empty result, every time. |
| [`scraper_without_stealth.py`](scraper_without_stealth.py) | Works. ~97 products in ~51 seconds. |

---

## The short version

**Do not install `playwright-stealth`.** It is the top answer in every forum
thread about scraping a Cloudflare-protected site, and it is the reason those
threads do not work.

`playwright-stealth` works by *overwriting* browser properties with generic
approximations — `navigator.webdriver`, the plugin list, the MIME types, the
WebGL vendor string, and about a dozen more.

Real Chrome already reports those correctly. So the library replaces accurate
values with generic ones, and **a fingerprint that is oddly clean and oddly
uniform is itself the signal.** Worse, its `window.chrome` shim and WebGL
vendor string are byte-identical in every copy of it on the internet, so the
"unique" fingerprint it manufactures is shared by thousands of bots — and real
users almost never have a fingerprint that matches anything.

Delete the four lines. That is the fix.

## The four lines

```diff
+ from playwright_stealth import Stealth
...
+   stealth = Stealth()
+   await stealth.apply_stealth_async(page)
```

## What actually made it work

Nothing exotic. Subtraction, plus three deliberate choices:

```python
browser = await pw.chromium.launch(
    channel="chrome",     # the Chrome already on the machine, not Chromium
    headless=False,       # a real window is the strongest "human" signal there is
)
context = await browser.new_context(
    locale="en-US",       # must agree with timezone_id and the site's region
    timezone_id="America/New_York",
)
```

**`channel="chrome"`** — the installed browser has a correct TLS/HTTP2
fingerprint, font stack and media codecs out of the box. Bundled Chromium does
not, and you would spend weeks trying to imitate them.

**`headless=False`** — headless is the first thing to change and the last thing
to give up.

**Locale and timezone that agree with the site** — an `en-US` `Accept-Language`
from an address on another continent is a contradiction nobody needs to make.

## The failure mode that costs people the most time

**Cloudflare's interstitial returns HTTP 200** (sometimes 403, sometimes 200).
The challenge page *is* a successful response.

That means a scraper built on "did the request work?" sees nothing wrong at
all. The selectors simply match nothing, the script writes an empty JSON file,
and it exits `0`. Two hours later you are debugging CSS selectors when the real
answer is that you were blocked forty minutes ago.

So this scraper **fails loudly**:

```
  CLOUDFLARE CHALLENGE DETECTED - nothing was scraped.
  HTTP status : 200 OK - the challenge is itself a successful response
  Page title  : 'Just a moment...'
  Signals     : iframe:challenges.cloudflare.com/..., title:just a moment,
                body:performing security verification
  Screenshot  : challenge_screenshots/challenge-20261013-113037.png
```

It writes `status: "blocked_by_cloudflare"` to the output, prints an ordered
list of fixes, saves a screenshot as evidence, and **exits non-zero** so CI
notices. It refuses to write an empty results file that looks like success.

"No results" and "I am being blocked" are completely different bugs. Tell them
apart.

---

## Install and run

```bash
pip install playwright
playwright install chrome

python scraper_without_stealth.py
```

You need real Chrome installed. A visible window will open.

To see the failure for yourself:

```bash
pip install playwright-stealth
python scraper_with_stealth.py
```

## Using it on another site

Everything is a flag — no need to edit the file.

```bash
python scraper_without_stealth.py \
    --url "https://example.com" \
    --term "monitor" \
    --limit 50 \
    --locale "en-GB" \
    --timezone "Europe/London" \
    --output results.json
```

| Flag | Why you want it |
| --- | --- |
| `--url` `--term` | What to scrape |
| `--locale` `--timezone` | **Set these to match the site.** The most important flag after `--url`. |
| `--limit` | `0` for no cap |
| `--profile DIR` | Persistent profile, so Cloudflare's `cf_clearance` cookie survives between runs |
| `--product-xpath` | Your selector for product links |
| `--search-xpath` | Your selector for the search box |
| `--headless` | Force headless. Works less often — the default is headful for a reason |
| `--ignore-robots` | Skip the `robots.txt` check. Only if you have permission |
| `--keep-open` | Leave the browser open at the end |

Exit codes: `0` success · `1` blocked or total failure · `2` robots.txt
disallowed · `3` selector matched nothing (a selector bug, not a block).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Empty results, exit `0` | You don't detect challenges | You need this repo's `detect_challenge`. Never treat "no rows" as success |
| `Just a moment...` | Fingerprint or profile problem | Delete the profile dir. Then headful, then `channel="chrome"` |
| Worked, then stopped working | The profile got poisoned | Delete the profile dir. This is the single most common fix |
| Blocked only on some pages | Per-page scoring | Challenge can appear on a product page, not just the results page — check every page |
| Blocked on the results page only | Navigation triggers scoring | The landing page is fine and the results page is not. Check both |
| Works at home, fails at work | Datacenter IP reputation | Nothing in the client will help. The decision happens before scoring |
| `no_links_matched` (exit 3) | Wrong XPath | Open DevTools, copy the real selector, pass `--product-xpath` |

**When deleting the profile is the answer:** a Chrome profile that has absorbed
a week of challenged attempts becomes associated with a bot and keeps getting
challenged even after your code is fixed. Delete the directory before you
debugging anything else. This is the step everybody skips.

---

## Other things worth stealing

These are good practice whether or not you ever touch a protected site:

- **Read structured data before the DOM.** JSON-LD `Product` schema and
  Next.js `__NEXT_DATA__` outlive the markup that renders them, so a redesign
  breaks your selectors long before it breaks these. The scraper tries both
  first and falls back to selectors.
- **Checkpoint as you go.** A 90-product run that dies at product 70 should
  not lose the first 69. It saves every 10.
- **Retry navigation with backoff.** Transient network failures should not end
  a run.
- **Deduplicate by URL, preserving order.**
- **Set a real User-Agent** and a delay between requests.
- **Check `robots.txt`.** Enabled by default; `--ignore-robots` if you have
  permission to.

## What this is not

- **Not a permanent bypass.** Countermeasures ship continuously. What changed
  is that a normal run works and a blocked one is obvious.
- **Not a CAPTCHA solver.** Deliberately out of scope. Defeating a mechanism
  whose entire purpose is telling humans from bots is not engineering, and a
  scraper that depends on it is one policy change from broken.
- **Not IP-proof.** On a flagged datacenter address the decision is made before
  the client is ever scored.
- **Not a bypass of access control to anything private.** Everything here
  targets publicly accessible listings.

## Responsible use

Only scrape what you are allowed to scrape. Respect the target's `robots.txt`
and terms of service, keep request rates low, identify yourself honestly, and
prefer an official API when one exists. The site used here is only a public
demonstration of a fingerprinting problem.

## Licence

Do what you like with it. If you use it, do not add stealth.