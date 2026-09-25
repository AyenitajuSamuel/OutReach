# Local Business Lead Finder & Outreach Toolkit

Two tools, used together, for finding small UK businesses that don't have a
website (or that already have one but no one managing it), and reaching out
to them without doing everything by hand:

1. **`project_terminator.py`** — a Selenium script that pulls business leads (name,
   phone, address, website, rating, etc.) from Google Maps search results
   into a CSV.
2. **Outreach Queue** — a single-page web app that reads that CSV, builds a
   personalized message per business, and gives you a one-click "open in
   WhatsApp" flow so you're not typing every message and number by hand.

---

## ⚠️ Before you use this

- **Scraping Google Maps violates Google's Terms of Service.** This is a
  personal/small-scale tool, not something to run at high volume or on a
  schedule. For anything serious or unattended, use the official
  [Google Places API](https://developers.google.com/maps/documentation/places/web-service/overview)
  instead.
- **Google can and will rate-limit or serve a reduced "limited view" of Maps**
  to sessions it flags as automated. The script includes pacing delays and
  retry logic to reduce this, but it isn't bulletproof.
- **WhatsApp's Terms of Service prohibit bulk/automated messaging.** The
  Outreach Queue tool deliberately does **not** auto-send anything — it only
  pre-fills a message and opens WhatsApp for you to hit send yourself, one
  chat at a time, to stay within normal usage. Sending too many *new* chats
  to strangers in a short window can still get a WhatsApp account
  temporarily restricted regardless of how the chat was opened — pace
  yourself (a handful a day, not dozens in one sitting).
- Cold outreach to UK businesses is generally fine under B2B rules
  (PECR), but always identify yourself clearly and keep messages relevant to
  their trade.

---

## Part 1 — `project_terminator.py`

### What it does

Given a search query (e.g. `"landscaping companies in Bristol UK"`), it:

1. Opens Google Maps and searches the query (optionally at a fixed
   lat/lng/zoom so results match what you'd see browsing manually).
2. Scrolls the results list, extracting new business cards as they load,
   until it hits your target count, Google's own list stops growing, or a
   safety cap is reached.
3. Clicks into each business one at a time and extracts: name, address,
   phone, website, Google Maps link, category, star rating, review count,
   opening hours, and shopping/pickup/delivery flags.
4. Saves everything to a CSV.

### Requirements

- Python 3.9+
- Google Chrome installed
- The following Python packages:

```bash
pip install selenium webdriver-manager
```

(`argparse`, `csv`, `dataclasses`, `logging`, `re`, `random`, and `time` are
all part of the Python standard library — no install needed.)

### Basic usage

```bash
python project_terminator.py-s "plumbers in london uk" -t 100
```

This searches for "plumbers in london uk", tries to collect up to 100
listings, and saves them to `plumbers_in_london_uk_deep_leads.csv` in the
current folder.

### All CLI options

| Flag | Default | What it does |
|---|---|---|
| `-s`, `--search` | `"plumbers in london uk"` | The search query to run on Google Maps. |
| `-t`, `--total` | `50` | Target number of listings to collect. The scraper stops scrolling once it hits this (or Google's own list runs out first). |
| `-o`, `--output` | auto-generated | Output CSV path. If omitted, it's derived from your search query (spaces/special characters replaced). |
| `--headless` | off | Runs Chrome without a visible window. Useful for background runs; leave it off while debugging so you can see what's happening. |
| `--verbose` | off | Enables debug-level logging (more detail on every step). |
| `--wait-timeout` | `15` | Seconds to wait for any single element/page action before timing out. Raise this (e.g. `30`) for slow-loading categories or a slow connection. |
| `--max-scroll-attempts` | `40` | Hard safety cap on scroll attempts, regardless of other settings. Prevents an infinite loop if something goes wrong. |
| `--stall-limit` | `3` | How many consecutive "no new listings" checks (each with a retry nudge) before the scraper concludes scrolling is done. Raise this (e.g. `6–10`) for categories that load slowly or in bursts. |
| `--min-delay` / `--max-delay` | `1.0` / `2.5` | Random pause range (seconds) between processing each business. This is deliberate pacing to look less like a bot and reduce Google's rate-limiting — don't set this to `0`. |
| `--lat`, `--lng`, `--zoom` | none / none / `12` | Pin the map to a specific center point and zoom level instead of letting Google auto-pick one after your search. **This matters a lot** — Google Maps only shows businesses in the *current visible map area*, so if you browsed manually at a wider zoom than the scraper's auto-zoom, you'll get fewer results than you saw by hand. Get `lat,lng,zoom` by browsing Google Maps yourself to the view you want, then copying the numbers out of the URL (they appear as `@51.4545,-2.5879,11z`). |
| `--no-website-only` | off | Filters the final saved CSV down to only businesses with no website listed — your actual outreach targets. |

### Example: matching a manual Google Maps view

```bash
python project_terminator.py \
  -s "landscaping companies in Bristol UK" \
  -t 150 \
  --max-scroll-attempts 80 \
  --stall-limit 8 \
  --wait-timeout 30 \
  --min-delay 1.5 --max-delay 3 \
  --lat 51.4545 --lng -2.5879 --zoom 11 \
  --no-website-only
```

### Output CSV columns

| Column | Meaning |
|---|---|
| `name` | Business name |
| `address` | Street address, if listed |
| `website` | Business website URL, blank if none found |
| `phone_number` | Phone number as shown on the listing |
| `map_link` | Direct Google Maps link to that business |
| `place_type` | Category (e.g. "Plumber", "Landscaper") |
| `reviews_average` | Star rating (e.g. `4.8`) |
| `reviews_count` | Number of reviews |
| `opens_at` | Raw opening-hours snippet from the listing, if available |
| `store_shopping`, `in_store_pickup`, `store_delivery` | `Yes`/`No` flags Google shows for some retail-type listings |
| `verified` | `Yes` normally. `No` means the detail pane never confirmed it had switched to this business before extraction — likely due to Google rate-limiting the session. When `No`, only `name` and `map_link` are populated; contact fields are deliberately left blank rather than risk attaching the *wrong* business's phone/address to this name. **Always manually check `verified: No` rows on Maps directly before contacting them.** |

### How it avoids common scraping pitfalls (for anyone reading the code)

- **Adaptive scrolling with a real wheel event**, not just a `scrollTop`
  jump — Google's lazy-loading can stop responding to repeated identical
  scroll positions, so the scraper dispatches an actual wheel event and
  "nudges" (scrolls up slightly, then back down) before concluding the list
  has genuinely stopped growing.
- **Waits for the detail heading to actually change** before extracting a
  business's data, not just for the element to "exist" — Google Maps reuses
  the same DOM node across clicks, so a naive presence-check can read
  stale/leftover data from the *previous* business.
- **Reads the map link from the browser URL after the detail pane loads**,
  rather than from the list card's `href` attribute before clicking, since
  that attribute can be momentarily inconsistent during virtualized-list
  scrolling.
- **Deduplicates** on name + address, since Google sometimes lists the same
  business more than once (ads, multiple branches).

---

## Part 2 — Outreach Queue (web tool)

A single-page app (no install, runs in your browser) that turns the CSV from
the scraper into a working outreach queue.

**Live link:** *(paste your published artifact link here once you have it)*

### What it does

1. **Upload the CSV** the scraper produced — parsed entirely in your
   browser; nothing is uploaded to a server.
2. **Pick a campaign**:
   - *No-website leads* — filters to businesses missing a website, for
     pitching a brand-new site.
   - *Has-website leads* — filters to businesses that already have one, for
     pitching ongoing website management/maintenance instead.
3. **Set your details once** (name/alias, email, a short sign-off line) —
   these get filled into every message automatically.
4. **Edit the message template** using placeholders:
   - `{name}` — business name
   - `{type}` — business category
   - `{rating}` — star rating
   - `{reviews}` — review count
   - `{yourName}`, `{yourEmail}`, `{yourContact}` — your own details
5. **Work through the queue one lead at a time**:
   - **Open in WhatsApp** — opens `wa.me` with the message pre-filled; you
     click send yourself.
   - **Copy message** — copies the current message to your clipboard.
   - **Mark as sent** — logs it and jumps to the next pending lead.
   - **Skip** — moves on without counting it as contacted.
6. **Progress is saved automatically** in your browser (per-device) — close
   the tab and come back later without losing your place.
7. **Download progress (CSV)** — exports a record of what's been contacted.

### Privacy note

Everything (the CSV, phone numbers, your saved progress) stays in your
browser's local storage. Nothing is sent to any server. This is also why
progress doesn't sync across devices — it's tied to the specific browser you
used.

### A note on WhatsApp numbers from outside the UK

If you're messaging UK numbers from a non-UK number, some recipients may be
more hesitant to engage with an unfamiliar international number — it can
read the same as a scam-text pattern, even though it isn't one. Two options:

- Rent a UK virtual number (e.g. via Twilio) so messages come from something
  that looks local.
- Lead every message by naming the actual business and something specific
  you noticed about them — specificity, not the number itself, is what
  reads as "a real person looked at my business."

---

## Suggested end-to-end workflow

1. Run `project_terminator.py` for your target trade + city.
2. Open the CSV, sanity-check a handful of rows (especially any marked
   `verified: No`).
3. Load the CSV into Outreach Queue.
4. Pick the No-website campaign, work through those leads first — they're
   the clearest pitch.
5. Switch to the Has-website campaign for the same city and go again with
   the website-management pitch.
6. Supplement with `site:facebook.com "<trade>" <city>` Google searches —
   public Facebook Business Pages often list a real, unmasked email address
   directly in their About section, which Maps never gives you.
7. Pace outreach out over days, not one sitting — for both WhatsApp account
   health and just generally not looking like a mass blast.

---

## Known limitations

- Google Maps' web UI caps out at roughly 100–120 results for any single
  search, logged in or not — splitting into narrower searches (by
  neighbourhood/postcode) is the only way past this for a given category.
- Selenium selectors here are tied to Google's current DOM structure/class
  names, which change without notice. If extraction suddenly returns empty
  fields across the board, the selectors likely need updating.
- This is built and tested for **Google Chrome** only (via
  `webdriver-manager`'s ChromeDriver).
