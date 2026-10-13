#!/usr/bin/env python3
"""
scraper_with_stealth.py — the version that FAILS.

This file is scraper_without_stealth.py plus a stealth import and three lines
that apply it. That is the entire difference. Run it against the same target
and it gets blocked by Cloudflare every single time and returns nothing.

    diff scraper_without_stealth.py scraper_with_stealth.py

Four added lines. That is the whole lesson, and it runs opposite to how almost
everyone writes about this:

    playwright-stealth works by OVERWRITING browser properties with generic
    approximations - navigator.webdriver, the plugin list, the MIME types, the
    WebGL vendor string, and a dozen more.

    Real Chrome already reports those correctly. Patching them replaces
    accurate values with generic ones, and Cloudflare scores exactly that
    mismatch: a fingerprint that is oddly clean and oddly uniform is itself the
    signal.

    Worse, the library's window.chrome shim and its WebGL vendor string are
    byte-identical in every copy of it on the internet, so the "unique"
    fingerprint it manufactures is shared by thousands of bots. Real users
    almost never have a fingerprint that matches.

Delete the four lines. That is the fix.

Anything else in here worth reading on its own: the challenge detection, the
loud failure, the checkpointing and the JSON-LD-first extraction are all still
there, because those are good practice regardless. See README.md.
"""

import sys

try:
    from playwright_stealth import Stealth
except ImportError:
    print(
        "\n"
        "  This file needs the library it exists to demonstrate:\n"
        "\n"
        "      pip install playwright-stealth\n"
        "\n"
        "  Then run it again and watch it get blocked anyway.\n"
        "  scraper_without_stealth.py needs nothing extra, and works.\n",
        file=sys.stderr,
    )
    raise SystemExit(2)

# Import the entire working implementation, then re-run it with stealth on.
#
# Doing it this way keeps the two files honest: there is exactly one copy of
# the scraper logic, so the only thing being demonstrated is the four lines
# above. If you had copied the file instead, the two would drift apart and the
# comparison would quietly stop meaning anything.
from scraper_without_stealth import *  # noqa: F401,F403
from scraper_without_stealth import Config, main

import scraper_without_stealth as _core


async def _apply_stealth(page):
    """The line that started the whole week of problems."""
    print("[+] Applying playwright-stealth...")
    stealth = Stealth()
    await stealth.apply_stealth_async(page)


# The only hook we need: apply stealth the moment the page exists, then carry
# on exactly as the working version does.
_original_run_search = _core.run_search


async def run_search(page, term, search_xpath):
    # >>> THIS IS THE ONLY FUNCTIONAL DIFFERENCE IN THE REPOSITORY <<<
    await _apply_stealth(page)
    # >>> END OF DIFFERENCE <<<
    return await _original_run_search(page, term, search_xpath)


_core.run_search = run_search


if __name__ == "__main__":
    import asyncio

    try:
        sys.exit(asyncio.run(_core.main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        sys.exit(130)