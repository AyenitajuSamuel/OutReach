"""
Google Maps scraper (Selenium) — hardened version.

Fixes applied:
1. Adaptive scrolling: scrolls until the listing count stabilizes or the
   target count is hit, with a max-attempt safety cap (no more fixed scrolls=10).
2. All time.sleep() calls replaced with WebDriverWait + expected_conditions.
3. Real exception logging (logging.exception with repr(e)) instead of bare
   except/pass or bare print statements.
4. Expanded fields to match Script 1's richness: address, rating, review
   count, opening hours, place type, and shopping/pickup/delivery flags.
5. Sanitized output filename (strips characters invalid on Windows/Linux/macOS).
6. Full argparse CLI: search query, output path, scroll/total count, headless,
   max scroll attempts, verbosity.

Still true regardless of hardening: this depends on Google's DOM structure,
which changes without notice, and scraping Maps at scale violates Google's
Terms of Service. For anything unattended or high-volume, use the official
Places API instead.
"""

import argparse
import csv
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Lead:
    name: str = ""
    website: str = ""
    phone_number: str = ""
    map_link: str = ""
    address: str = ""
    place_type: str = ""
    reviews_average: Optional[float] = None
    reviews_count: Optional[int] = None
    opens_at: str = ""
    store_shopping: str = "No"
    in_store_pickup: str = "No"
    store_delivery: str = "No"
    verified: str = "Yes"  # "No" means the detail pane never confirmed loading this
    # business's own data before extraction — contact fields are left blank rather
    # than risk attaching a different business's stale phone/address/website.


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


# --------------------------------------------------------------------------- #
# Driver setup
# --------------------------------------------------------------------------- #

def build_driver(headless: bool = False) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--lang=en-US")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    # Reduces "DevTools listening on ws://..." noise and some automation flags
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    return driver


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #

def safe_find_text(driver, by, value, attribute=None):
    """Return element text (or an attribute), or '' on any failure. Never raises."""
    try:
        el = driver.find_element(by, value)
        if attribute:
            return (el.get_attribute(attribute) or "").strip()
        return (el.text or "").strip()
    except NoSuchElementException:
        return ""
    except Exception as e:
        logging.debug("safe_find_text failed for %s=%s: %r", by, value, e)
        return ""


WEBSITE_XPATH = '//a[contains(@data-item-id, "authority")] | //a[contains(@aria-label, "Website:")]'


def extract_website(driver, website_wait_seconds: int = 4) -> str:
    """
    The website chip is often the LAST thing to attach in the detail panel —
    Google resolves/validates the URL after the rest of the place data (name,
    address, phone, rating) has already rendered. A single immediate check can
    therefore record "" for a business that genuinely has a website but just
    hadn't shown it yet, which is indistinguishable from a true no-website
    listing unless we give it a fair, dedicated chance to appear.
    """
    site = safe_find_text(driver, By.XPATH, '//a[contains(@data-item-id, "authority")]', attribute="href")
    if not site:
        site = safe_find_text(driver, By.XPATH, '//a[contains(@aria-label, "Website:")]', attribute="href")
    if site:
        return site

    # Not found on the first pass — give it a short, separate window to render
    # before concluding this is a real no-website lead.
    try:
        el = WebDriverWait(driver, website_wait_seconds).until(
            EC.presence_of_element_located((By.XPATH, WEBSITE_XPATH))
        )
        return el.get_attribute("href") or ""
    except TimeoutException:
        return ""  # genuinely no website, as far as we can tell


def extract_lead(driver, map_link: str) -> Lead:
    lead = Lead(map_link=map_link)

    # Name — primary + fallback selector
    lead.name = (
        safe_find_text(driver, By.XPATH, '//h1[contains(@class,"DUwDvf")]')
        or safe_find_text(driver, By.XPATH, '//div[@class="TIHn2 "]//h1')
    )

    # Address
    lead.address = (
        safe_find_text(driver, By.XPATH, '//button[@data-item-id="address"]//div[contains(@class,"fontBodyMedium")]')
        or safe_find_text(driver, By.XPATH, '//button[contains(@aria-label, "Address:")]', attribute="aria-label")
    )
    if lead.address.lower().startswith("address:"):
        lead.address = lead.address.split(":", 1)[1].strip()

    # Phone
    phone_raw = safe_find_text(driver, By.XPATH, '//button[contains(@data-item-id, "phone:")]', attribute="aria-label")
    if not phone_raw:
        phone_raw = safe_find_text(driver, By.XPATH, '//button[contains(@aria-label, "Phone:")]', attribute="aria-label")
    lead.phone_number = phone_raw.replace("Phone:", "").strip() if phone_raw else ""

    # Website — see extract_website() for why this needs its own dedicated wait.
    lead.website = extract_website(driver)

    # Place type (category, e.g. "Turkish restaurant")
    lead.place_type = safe_find_text(driver, By.XPATH, '//div[@class="LBgpqf"]//button[contains(@class,"DkEaL")]')

    # Reviews count + average rating
    try:
        rating_el = driver.find_element(By.XPATH, '//div[@class="TIHn2 "]//div[contains(@class,"fontBodyMedium")]//span[@aria-hidden]')
        raw = (rating_el.text or "").replace(",", ".").strip()
        if raw:
            lead.reviews_average = float(re.sub(r"[^\d.]", "", raw))
    except Exception as e:
        logging.debug("Rating parse failed: %r", e)

    try:
        count_el = driver.find_element(By.XPATH, '//div[@class="TIHn2 "]//span[@aria-label and contains(@aria-label,"reviews")]')
        raw = count_el.get_attribute("aria-label") or ""
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            lead.reviews_count = int(digits)
    except Exception as e:
        logging.debug("Review count parse failed: %r", e)

    # Opening hours
    lead.opens_at = (
        safe_find_text(driver, By.XPATH, '//button[contains(@data-item-id, "oh")]//div[contains(@class,"fontBodyMedium")]')
        or safe_find_text(driver, By.XPATH, '//div[@class="MkV9"]//span[@class="ZDu9vd"]//span[2]')
    )
    if "⋅" in lead.opens_at:
        lead.opens_at = lead.opens_at.split("⋅", 1)[1].replace("\u202f", "").strip()

    # Shopping / pickup / delivery flags (Google shows these as small info chips)
    for idx in (1, 2, 3):
        info_text = safe_find_text(driver, By.XPATH, f'(//div[@class="LTs0Rc"])[{idx}]')
        if info_text and "·" in info_text:
            check = info_text.split("·", 1)[1].lower()
            if "shop" in check:
                lead.store_shopping = "Yes"
            if "pickup" in check:
                lead.in_store_pickup = "Yes"
            if "delivery" in check:
                lead.store_delivery = "Yes"

    return lead


# --------------------------------------------------------------------------- #
# Consent dialog
# --------------------------------------------------------------------------- #

def dismiss_consent_dialog(driver, wait: WebDriverWait):
    try:
        consent = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[.//span[contains(text(), 'Accept all') or contains(text(), 'Reject all')]]")
            )
        )
        consent.click()
        logging.info("Dismissed consent dialog.")
    except TimeoutException:
        logging.debug("No consent dialog appeared (or it timed out) — continuing.")
    except Exception as e:
        logging.warning("Unexpected error dismissing consent dialog: %r", e)


# --------------------------------------------------------------------------- #
# Adaptive scrolling
# --------------------------------------------------------------------------- #

def _scroll_feed(driver, feed):
    """
    Scroll the results panel toward the bottom.

    A plain `scrollTop = scrollHeight` jump works for the first few loads, but
    once already parked at the bottom, repeating the exact same jump can stop
    reliably re-triggering Google's lazy-load listener — the panel's infinite
    scroll appears to key off actual wheel/scroll events, not just the raw
    scrollTop value settling at a new number. Dispatching a real wheel event
    is more reliable than the scrollTop assignment alone.
    """
    driver.execute_script(
        """
        const el = arguments[0];
        el.dispatchEvent(new WheelEvent('wheel', {deltaY: 4000, bubbles: true}));
        el.scrollTop = el.scrollHeight;
        """,
        feed,
    )


def scroll_results(driver, wait: WebDriverWait, total: int, max_attempts: int = 50, stall_limit: int = 3) -> int:
    """
    Scroll the results feed until the listing count reaches `total`, or stabilizes
    for `stall_limit` consecutive checks (each with a nudge-retry first), or
    `max_attempts` is hit (safety valve). Returns the number of listing cards found.
    """
    card_xpath = "//a[contains(@href, 'https://www.google.com/maps/place/')]"
    end_of_list_xpath = "//span[contains(text(), \"You've reached the end of the list\")]"

    try:
        feed = wait.until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(@aria-label, 'Results for') or @role='feed']"))
        )
    except TimeoutException:
        logging.error("Could not locate the results feed to scroll.")
        return 0

    previously_counted = 0
    stall_count = 0

    for attempt in range(1, max_attempts + 1):
        _scroll_feed(driver, feed)

        # FIX: this must wait for MORE cards than before (>), not >= previously_counted.
        # previously_counted starts at 0 (and never decreases), so ">=" was true
        # instantly on every attempt — the wait never actually waited for lazy-load,
        # so the count was sampled before new cards rendered, which faked a "stall"
        # after only 3 attempts and stopped the scroll around whatever Maps renders
        # on first paint (~20 cards).
        try:
            wait.until(lambda d: len(d.find_elements(By.XPATH, card_xpath)) > previously_counted)
        except TimeoutException:
            # A real timeout here means no new cards loaded within wait_timeout —
            # try a "nudge": scroll up a bit and back down. This sometimes wakes
            # the lazy loader back up when it's stopped responding at the bottom.
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop - 400;", feed)
            try:
                wait.until(EC.presence_of_element_located((By.XPATH, card_xpath)))
            except TimeoutException:
                pass
            _scroll_feed(driver, feed)
            try:
                wait.until(lambda d: len(d.find_elements(By.XPATH, card_xpath)) > previously_counted)
            except TimeoutException:
                pass  # genuinely no growth even after the nudge — let stall logic below decide

        # Google shows an explicit end-of-results marker — stop cleanly if we hit it.
        if driver.find_elements(By.XPATH, end_of_list_xpath):
            found = len(driver.find_elements(By.XPATH, card_xpath))
            logging.info("Reached end of list marker at %d results.", found)
            return found

        found = len(driver.find_elements(By.XPATH, card_xpath))
        logging.info("Scroll attempt %d — currently found: %d", attempt, found)

        if found >= total:
            return found
        if found == previously_counted:
            stall_count += 1
            if stall_count >= stall_limit:
                logging.info("Listing count stalled at %d after %d checks — stopping.", found, stall_count)
                return found
        else:
            stall_count = 0
        previously_counted = found

    logging.warning("Hit max scroll attempts (%d) without reaching target.", max_attempts)
    return previously_counted


# --------------------------------------------------------------------------- #
# Main scrape routine
# --------------------------------------------------------------------------- #

def scrape_gmaps_deep(
    search_query: str,
    total: int = 50,
    headless: bool = False,
    max_scroll_attempts: int = 50,
    wait_timeout: int = 15,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    zoom: Optional[float] = None,
    stall_limit: int = 3,
    min_delay: float = 1.0,
    max_delay: float = 2.5,
) -> List[Lead]:
    logging.info("Searching for: %s", search_query)

    driver = None
    leads: List[Lead] = []
    seen_keys = set()

    try:
        driver = build_driver(headless=headless)
        wait = WebDriverWait(driver, wait_timeout)

        if lat is not None and lng is not None:
            # Pin the exact map center/zoom instead of letting Google auto-pick
            # one on search — results are viewport-bound, so a tighter auto-zoom
            # than what you'd use browsing manually is the #1 reason the scraper
            # returns fewer businesses than you see by hand.
            zoom_val = zoom if zoom is not None else 12
            query_slug = search_query.strip().replace(" ", "+")
            url = f"https://www.google.com/maps/search/{query_slug}/@{lat},{lng},{zoom_val}z"
            logging.info("Using fixed viewport: %s", url)
            driver.get(url)
            dismiss_consent_dialog(driver, WebDriverWait(driver, 3))
        else:
            driver.get("https://www.google.com/maps")
            wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='searchboxinput' or @name='q']")))

            dismiss_consent_dialog(driver, WebDriverWait(driver, 3))

            search_box = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='searchboxinput' or @name='q']")))
            search_box.clear()
            search_box.send_keys(search_query)
            search_box.send_keys(Keys.ENTER)

        card_xpath = "//a[contains(@href, 'https://www.google.com/maps/place/')]"
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, card_xpath)))
        except TimeoutException:
            logging.error("No results appeared for query %r — aborting.", search_query)
            return leads

        found = scroll_results(driver, wait, total, max_attempts=max_scroll_attempts, stall_limit=stall_limit)
        if found == 0:
            logging.error("No listings found after scrolling — aborting.")
            return leads

        total_targets = min(found, total)
        logging.info("Found %d targets. Extracting up to %d. This may take a few minutes...", found, total_targets)

        last_heading = None
        for i in range(total_targets):
            try:
                # Re-locate cards every loop — clicking re-renders the DOM and
                # stale references are the #1 cause of crashes in the original script.
                cards = driver.find_elements(By.XPATH, card_xpath)
                if i >= len(cards):
                    logging.warning("Card index %d no longer exists (DOM shrank) — stopping early.", i)
                    break
                card = cards[i]

                name_hint = card.get_attribute("aria-label") or f"listing {i + 1}"

                logging.info("Checking (%d/%d): %s", i + 1, total_targets, name_hint)

                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
                wait.until(EC.element_to_be_clickable((By.XPATH, f"({card_xpath})[{i + 1}]")))
                card.click()

                # Google Maps reuses the SAME <h1> DOM node across clicks (SPA behavior) —
                # it just updates the text in place. `presence_of_element_located` returns
                # instantly because the node is already there from the PREVIOUS business,
                # which means we'd read stale text before the new one finishes loading.
                # Wait for the heading text to actually be non-empty AND different from
                # what we last extracted, not merely "present".
                h1_xpath = '//h1[contains(@class,"DUwDvf")]'

                def _heading_updated(d, _prev=last_heading):
                    try:
                        txt = (d.find_element(By.XPATH, h1_xpath).text or "").strip()
                    except NoSuchElementException:
                        return False
                    return bool(txt) and txt != _prev

                try:
                    wait.until(_heading_updated)
                    heading_confirmed = True
                except TimeoutException:
                    logging.warning(
                        "Detail heading never changed from %r for %s — retrying click once.",
                        last_heading, name_hint,
                    )
                    heading_confirmed = False
                    try:
                        card.click()
                        wait.until(_heading_updated)
                        heading_confirmed = True
                    except (TimeoutException, StaleElementReferenceException) as e:
                        logging.warning(
                            "Retry also failed for %s (%r) — likely Google throttling this session. "
                            "Saving with list name only; contact fields left blank rather than risk "
                            "attaching a different business's stale phone/address.",
                            name_hint, e,
                        )

                # Read map_link from the URL AFTER the detail pane has loaded, rather than
                # from the list card's href captured before the click — the href attribute
                # on a virtualized list card can be momentarily stale/inconsistent relative
                # to the visible label at the instant you read it, which is what caused
                # rows with one business's name/phone but a different business's link.
                map_link = driver.current_url

                if heading_confirmed:
                    lead = extract_lead(driver, map_link)
                    if not lead.name:
                        lead.name = name_hint  # fall back to the card's aria-label
                else:
                    # Detail pane never confirmed it switched to this business — do NOT
                    # trust any of its contact fields, they likely belong to whichever
                    # business was showing before. Use the reliable list-provided name
                    # for identification, leave everything else blank, and flag it so
                    # you know to look this one up manually before messaging it.
                    lead = Lead(name=name_hint, map_link=map_link, verified="No")
                last_heading = lead.name

                # A deliberate, human-like pause. Not for waiting on rendering (the
                # explicit waits above handle that) — this is specifically to reduce
                # how often we trigger Google's own rate-limiting, which is what the
                # flat 60s-per-attempt scroll stalls and repeated heading-timeouts in
                # your logs look like.
                time.sleep(random.uniform(min_delay, max_delay))

                key = (lead.name.strip().lower(), lead.address.strip().lower())
                if key in seen_keys:
                    logging.info("Duplicate skipped: %s", lead.name)
                    continue
                seen_keys.add(key)
                leads.append(lead)

            except StaleElementReferenceException as e:
                logging.warning("Stale element at index %d, skipping: %r", i, e)
                continue
            except WebDriverException as e:
                logging.warning("WebDriver error at index %d, skipping: %r", i, e)
                continue
            except Exception as e:
                logging.exception("Unexpected error at index %d: %r", i, e)
                continue

    except TimeoutException as e:
        logging.error("Timed out during scrape setup (search box / results never appeared): %r", e)
    except WebDriverException as e:
        logging.error("WebDriver-level failure (browser/driver issue): %r", e)
    except Exception as e:
        logging.exception("Unexpected top-level error: %r", e)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as e:
                logging.debug("Error quitting driver (ignored): %r", e)

    return leads


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def sanitize_filename(name: str) -> str:
    """Strip characters invalid on Windows/Linux/macOS filesystems."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    return name or "leads"


def save_leads_to_csv(leads: List[Lead], output_path: str):
    if not leads:
        logging.warning("No leads to save — list is empty.")
        return

    fieldnames = list(asdict(leads[0]).keys())

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                writer.writerow(asdict(lead))
        logging.info("Saved %d leads to %s", len(leads), output_path)
    except OSError as e:
        logging.exception("Failed to write CSV to %s: %r", output_path, e)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Deep-scrape business leads from Google Maps (Selenium).")
    parser.add_argument("-s", "--search", type=str, default="childcare centers in Seattle Washington",
                         help="Search query for Google Maps")
    parser.add_argument("-t", "--total", type=int, default=150,
                         help="Total number of results to scrape")
    parser.add_argument("-o", "--output", type=str, default=None,
                         help="Output CSV file path (default: derived from search query)")
    parser.add_argument("--max-scroll-attempts", type=int, default=80,
                         help="Safety cap on scroll attempts before giving up")
    parser.add_argument("--stall-limit", type=int, default=10,
                         help="How many consecutive no-growth checks (after a nudge-retry each) before giving up on scrolling. Raise this for slow-loading categories. Default 10")
    parser.add_argument("--min-delay", type=float, default=1.5,
                         help="Minimum seconds to pause between businesses (human-like pacing to reduce rate-limiting). Default 1.5")
    parser.add_argument("--max-delay", type=float, default=2.5,
                         help="Maximum seconds to pause between businesses. Default 2.5")
    parser.add_argument("--wait-timeout", type=int, default=30,
                         help="Default explicit-wait timeout in seconds")
    parser.add_argument("--headless", action="store_true",
                         help="Run the browser headlessly")
    parser.add_argument("--verbose", action="store_true",
                         help="Enable debug-level logging")
    parser.add_argument("--no-website-only", action="store_true",
                         help="Only save leads that have no website listed (your actual outreach targets)")
    parser.add_argument("--lat", type=float, default=None,
                         help="Latitude to center the map on (pin the viewport instead of Google auto-zoom)")
    parser.add_argument("--lng", type=float, default=None,
                         help="Longitude to center the map on (use with --lat)")
    parser.add_argument("--zoom", type=float, default=12,
                         help="Zoom level for the pinned viewport (lower = zoomed out, more area covered). Default 12")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    output_path = args.output or f"{sanitize_filename(args.search)}_deep_leads.csv"

    leads = scrape_gmaps_deep(
        search_query=args.search,
        total=args.total,
        headless=args.headless,
        max_scroll_attempts=args.max_scroll_attempts,
        wait_timeout=args.wait_timeout,
        lat=args.lat,
        lng=args.lng,
        zoom=args.zoom,
        stall_limit=args.stall_limit,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
    )

    with_site = sum(1 for lead in leads if lead.website)
    without_site = len(leads) - with_site
    unverified = sum(1 for lead in leads if lead.verified == "No")
    logging.info(
        "Of %d leads: %d have a website listed, %d do not. %d are UNVERIFIED "
        "(detail pane never confirmed loading — name only, no contact info; check these manually before messaging).",
        len(leads), with_site, without_site, unverified,
    )

    if args.no_website_only:
        leads = [lead for lead in leads if not lead.website]
        logging.info("--no-website-only set: keeping %d leads with no website.", len(leads))

    save_leads_to_csv(leads, output_path)


if __name__ == "__main__":
    main()

# Run this code in the terminal if you want the scraper to scrape business without websites alone so just "your search title
# python project_terminator.py -s "your search title" -t 150 --max-scroll-attempts 80 --no-website-only