# Home Scout

A daily digest of **newly listed single-family homes** in your target California cities. Each house shows
a down-payment table (3% / 3.5% FHA / 5% / 10% / 20%) with the loan amount and estimated monthly payment, loan-limit and
Mello-Roos warnings, a CalHFA note, and there's local news per city. **Plain Python, no AI/LLM at runtime.**
Results are never filtered by price or affordability: every house that meets the beds/baths/type/lot criteria is shown.

## What it does each run
1. Gets the latest Freddie Mac 30-yr rate (FRED `MORTGAGE30US`, no key) and the change from the week before.
2. For each area in `config.yaml`, fetches for-sale listings from Realtor.com data using the
   [HomeHarvest](https://github.com/ZacharyHampton/HomeHarvest) library, with broad filters (3+ bd, 2+ ba, single-family, lot ≥ 5,000 sf, no price cap).
3. Saves listing IDs in `data/scout.db` (SQLite) so each report marks only **new** listings.
   The first run marks the last 7 days as "INITIAL".
4. Works out, per house and per down-payment level (3%, 3.5% FHA, 5%, 10%, 20%), the cash down, loan amount, and monthly payment
   (P&I at the current rate + 1.2% tax + insurance + PMI/FHA MIP + HOA), plus county loan-limit warnings and a Mello-Roos flag.
5. Pulls Google News RSS headlines per city (housing, development, schools, safety) from the last 3 days, with duplicates removed.
6. Writes `reports/YYYY-MM-DD.html` and `reports/latest.html` (plus the manifest and icons for "Add to Home Screen"). The page works on phones:
   one column, and the filter panel is collapsed by default. Filters run in the page with plain JavaScript: min/max price, beds, baths, lot slider,
   cities, new-only, and sort. Photos load from Realtor.com's image URLs (set `report.embed_photos: true` to embed them instead).
   If a city's fetch fails, the page still renders, with a red banner and that city's last-known listings.
7. Optionally emails the digest (turned off by default).

## Page sections
- **Tabs: All listings | ♥ Favorites (N).** Tap ♡ on any card to save it. Favorites live in your browser's localStorage
  (per device/browser) as a snapshot: address, price, beds/baths/sqft/lot, photos, link, down-payment table, and the date saved.
  A favorite stays even after the listing leaves the daily feed (marked **"No longer in today's feed"**). If it comes back at a different
  price, it shows **"Price changed: old → new"**. Remove one with ♥ in the Favorites tab. Use **Export/Import** to move favorites between
  phone and computer as a JSON file.
- **📰 Region news modules (top of page):** one swipeable card row per region in `locations.yaml` (`region:` = Tri-Valley / Altamont; a new
  location is assigned to the nearest region within `radius_km`, otherwise an "Other Areas News" module appears). Housing-market stories only,
  picked by keyword rules in `config.yaml → news_modules`: they must mention housing, prices, sales, inventory, mortgages, zoning or planning,
  the housing element, or builders, and crime, obituaries, sports, the Irish/Ohio Dublin, and people named Tracy are excluded. Stories come from
  the last 14 days, ranked by relevance and recency, up to 12 cards. Each card's **summary** is the publisher page's `og:description`.
  Google News links are resolved to the publisher URL with `googlenewsdecoder` and fetched once (8 s timeout), then cached in SQLite
  (`article_cache`). Tapping a card opens the publisher's article.
- **Photo carousel:** up to 15 photos per listing (Realtor.com 1024×768 variants), swipe on a phone, ‹ › arrows on desktop, with a "3/12" counter.
  Photos are lazy-loaded. Tapping a photo doesn't navigate away; "View on Realtor.com" is the link.
- **Search box**: filters the listings by city, zip, or street across every area. Typing a search also shows listings that aren't new.
- **Listings**: cards with the down-payment table (filters & sort are in the collapsible panel).
- **📈 Pricing in this area**: per area, the current median list price, median $/sqft, active inventory, and median days on market, taken from
  **all** active matching listings. The same numbers are saved daily in SQLite (`area_history` table), so 30- and 90-day change columns and SVG sparklines
  fill in as history builds up. It also shows recently sold comps (Realtor.com sold records via HomeHarvest, last 30 and 90 days).
- **🏗 New developments & new builds**: per area, development headlines from the last 14 days (Google News RSS, filtered by keyword rules:
  a development word plus the city or a regional term, with obituaries, elections, etc. excluded), plus **new builds on the market**
  (Realtor.com's new_construction flag, or built this year or last; any lot size; builder name when known).
- **🎯 First-time buyer watch**: about 12 statewide news searches (rates, CalHFA/Dream For All, down-payment assistance, AC Boost, loan limits, C.A.R.
  forecast, insurance, property tax, legislation), ranked by keyword weights in `config.yaml` → `buyer_watch.weights`. ★ marks the top ones.
  There's also a static Resources box.
- **📰 Area news & commute** and **📍 Locations** (drive times, plus an "Add or remove locations" button when hosted on GitHub).

## Locations
Areas live in `locations.yaml`. To add or remove them:
- **Phone/GitHub:** open the page → 📍 Locations → **Add or remove locations** (or repo → Actions → Home Scout daily → **Run workflow**), fill in
  `add_locations` (e.g. `Brentwood, CA, 95377`) and/or `remove_locations` (e.g. `Lathrop`), then Run. The workflow looks up the county (OpenStreetMap
  Nominatim), works out no-traffic drive times to San José and Modesto (OSRM), commits `locations.yaml`, and republishes the page.
- **Locally:** `python scout.py --add-location "Brentwood, CA" --remove-location "Lathrop"` (add `--locations-only` to skip the run).
- A zip is searched as-is, and its label shows the main city, e.g. `95377 (Tracy)`. A city is searched as "City, CA" and only results whose city matches are kept.
- `news_exclude` words per area cut news noise (e.g. Dublin → Ireland).

## Files
| Path | Purpose |
|---|---|
| `locations.yaml` | Areas to search (added and removed by CLI or the workflow form) |
| `hs_locations.py`, `hs_news.py`, `hs_market.py` | Locations/geocoding/drive times, news collection & ranking, pricing history & sold comps |
| `config.yaml` | **Everything you can change**: areas, filters, down-payment scenarios, finance assumptions, loan/income limits, news queries, assistance programs |
| `scout.py` | The program |
| `templates/report.html.j2` | HTML report template (Jinja2) plus the in-page JavaScript |
| `.github/workflows/scout.yml` | GitHub Actions: daily run + Pages publish (see "Run it on GitHub") |
| `web/` | Web manifest + icons (home-screen app) |
| `run.sh` | Wrapper used by cron. Loads `.env`, creates `.venv` if missing, logs to `logs/cron.log` |
| `crontab.txt` | Cron entry (6:00 AM Pacific) |
| `logs/home-scout.log`, `logs/cron.log` | Logs |
| `data/scout.db` | Listings already seen. Delete it to start fresh; the next run is "initial" again |

## Config highlights
- `search.*`: what gets **fetched**. Keep this broad; narrow it in the page.
- `view_defaults.*`: the page's starting filters (3 bd, 2 ba, lot ≥ 7,000 sf, new only, sorted by price).
- `finance.*`: `rate_override_pct`, tax rate (1.2%), insurance, PMI bands, FHA MIP, and the down-payment `scenarios` list.
- `counties.*`: 2026 conforming/FHA limits and CalHFA program income limits (shown as info only). **Update every January (loan limits) and June (CalHFA).**
- `areas[].location`: optional search override. Mountain House uses zip `95391` because "Mountain House, CA" returns nothing.
- `areas[].drive_min`: no-traffic drive minutes to downtown San José and Modesto (OSRM, measured 2026-10-20).

## Run it
```bash
./run.sh                 # normal run (updates the database, emails if enabled)
./run.sh --dry-run       # don't mark listings as seen
./run.sh --no-email
./run.sh --scheduled     # what GitHub uses: does nothing if it already ran today or it's before 6 AM Pacific
```
A run takes about 2.5 minutes (16 listing calls and about 100 news queries). Open `reports/latest.html` in any browser.

## Email (optional, off by default)
1. Turn on 2-Step Verification for Gmail, then create an App Password at https://myaccount.google.com/apppasswords
2. `cp .env.example .env` and fill it in (`chmod 600 .env`).
3. Set `email.enabled: true` in `config.yaml`.
The email has new listings only, with the HTML report attached for the interactive version.

## Scheduling
**On this Linux box (already installed):** `crontab -l` shows `0 6 * * * /workspace/home-scout/run.sh` with `CRON_TZ=America/Los_Angeles`.
The box has no init system, so if it restarts, the cron daemon is started again by a line in `~/.bashrc`
(or start it by hand with `sudo mkdir -p /var/run && sudo cron`).

**On your own computer:** install Python 3.10+ and copy this folder (leave out `.venv`), then:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```
- **macOS:** `crontab -e` and add `0 6 * * * /path/to/home-scout/run.sh`. For a run that catches up after sleep, use a launchd
  plist with `StartCalendarInterval` (Hour 6, Minute 0). Allow Terminal/cron Full Disk Access if the folder is in Documents.
- **Windows:** use `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, then Task Scheduler →
  Daily 6:00 AM → Program `C:\path\home-scout\.venv\Scripts\python.exe`, Arguments `scout.py`, Start in `C:\path\home-scout`.
  Tick "Run task as soon as possible after a scheduled start is missed".
- **Linux:** same cron line as above.

## Run it on GitHub (phone access)
GitHub Actions runs the scout every morning for free and publishes the report to GitHub Pages, so you can open it on your phone.
Your computer and this box don't need to be on.

1. **Create a repository.** On github.com (or the GitHub mobile app), make a new **public** repository called `home-scout`
   with no README, .gitignore, or license.
   (GitHub Pages on a *private* repo needs a paid plan. The page shows only public listing data, news, and your search settings, no personal info.)
2. **Push this folder.** From a computer that has this folder (e.g. download it from the box), run:
   ```bash
   cd home-scout
   git remote add origin https://github.com/<your-username>/home-scout.git
   git branch -M main
   git push -u origin main
   ```
   (or use `gh repo create home-scout --public --source . --push` if you have the GitHub CLI and are logged in.)
3. **Turn on Pages.** Repo → **Settings → Pages → Build and deployment → Source: "GitHub Actions"**.
4. **Allow the workflow to save state.** Repo → **Settings → Actions → General → Workflow permissions → "Read and write permissions"** → Save.
   This lets it commit `data/scout.db` each day, which is how it remembers which listings are new.
5. **First run.** Repo → **Actions → "Home Scout daily" → Run workflow**. In the GitHub mobile app: repo → Actions → workflow → "Run workflow".
   It takes about 3 minutes. Your page is at **`https://<your-username>.github.io/home-scout/`** (the link is also on the run's "deploy" step).
6. **Add to your phone's home screen.** Open the page in Safari → Share → **Add to Home Screen**. On Android/Chrome: ⋮ → **Add to Home screen**.
7. **Daily.** It runs automatically at ~6:00 AM Pacific. Cron jobs fire at 13:00 and 14:00 UTC to cover daylight saving, and only one of them runs.
   GitHub's scheduled runs can start 5–30+ minutes late. Tap **Run workflow** any time for a fresh report.
8. *(Optional email)* Repo → Settings → Secrets and variables → Actions: add `HOME_SCOUT_SMTP_USER`, `HOME_SCOUT_SMTP_PASSWORD`
   (a Gmail App Password), `HOME_SCOUT_EMAIL_TO`. Then set `email.enabled: true` in `config.yaml`.

**To change settings:** edit `config.yaml` on github.com (pencil icon, works on a phone too) and commit. The next run uses it.

**Notes:**
- The repo includes `data/scout.db`, so the first GitHub run continues from the box's last run. To start over, delete it and the next run is "initial" again.
- If Realtor.com blocks GitHub's servers, the page still publishes with a red "fetch failed" banner and last-known listings.
  You'd see that banner on the first run. If it happens, the fallback is running `run.sh` on your own computer (see Scheduling).
- GitHub turns off scheduled workflows in repos with no activity for 60 days. The daily state commit counts as activity, so this shouldn't happen.
- If the box and GitHub both run, each keeps its own `scout.db`, so "new" is tracked separately.

## Caveats
- HomeHarvest reads Realtor.com's site data. It isn't an official API, so keep it to personal use and low volume. It may break
  if Realtor.com changes things (`pip install -U homeharvest` usually fixes it). Backup sources: RentCast API (free tier is 50 requests/month).
- All payment numbers are estimates. Mello-Roos/CFD taxes, HOA accuracy, insurance quotes, and your
  rate/credit can change them a lot. Confirm with a CalHFA-approved lender.
- Google News RSS is for personal, non-commercial use.
