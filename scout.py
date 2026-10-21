#!/usr/bin/env python3
"""Home Scout - daily new-listing digest. Plain Python, no AI at runtime.

Sources: HomeHarvest (Realtor.com data), FRED MORTGAGE30US (Freddie Mac PMMS), Google News RSS.
"""
import argparse, base64, csv, datetime as dt, email.utils, html, io, json, logging, math, os, re
import shutil, smtplib, sqlite3, statistics, sys, time, urllib.parse, xml.etree.ElementTree as ET
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests, yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) HomeScout/1.0 (personal use)"}
log = logging.getLogger("home-scout")


# ----------------------------------------------------------------- utils
def now_local():
    return dt.datetime.now(TZ)

def num(v):
    """Convert pandas/NA-ish values to float or None."""
    try:
        if v is None: return None
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None

def s(v):
    if v is None: return ""
    t = str(v)
    return "" if t in ("nan", "<NA>", "None", "NaT") else t


# ----------------------------------------------------------------- finance
def amort(loan, rate_pct, years):
    r = rate_pct / 100 / 12; n = years * 12
    if r == 0: return loan / n
    return loan * r / (1 - (1 + r) ** -n)

def pmi_pct(fin, down):
    if down >= 20: return 0.0
    bands = sorted((float(k), float(v)) for k, v in fin["pmi_annual_pct"].items())
    pct = bands[0][1]
    for k, v in bands:
        if down >= k: pct = v
    return pct

def monthly_cost(price, down, typ, rate, fin, county, hoa=0.0):
    """Return dict with monthly breakdown for one scenario."""
    d = down / 100
    base = price * (1 - d)
    out = {"down": price * d, "base_loan": base}
    if typ == "fha":
        loan = base * (1 + fin["fha_upfront_mip_pct"] / 100)
        r = rate - fin.get("fha_rate_discount_pct", 0)
        hi = base > fin["fha_high_balance_threshold"]
        mip_pct = fin["fha_annual_mip_pct_high" if hi else "fha_annual_mip_pct"]
        if down >= 5: mip_pct -= 0.05
        mi = base * mip_pct / 100 / 12
        out["over_limit"] = base > county["fha_limit"]
        out["limit_label"] = "FHA limit"
    else:
        loan = base; r = rate
        mi = loan * pmi_pct(fin, down) / 100 / 12
        out["over_limit"] = loan > county["conforming_limit"]
        out["limit_label"] = "conforming limit (jumbo above)"
    pi = amort(loan, r, fin["term_years"])
    tax = price * fin["property_tax_rate_pct"] / 100 / 12
    ins = fin["insurance_annual"] / 12
    out.update(loan=loan, pi=pi, mi=mi, tax=tax, ins=ins, hoa=hoa or 0.0)
    out["total"] = pi + mi + tax + ins + (hoa or 0.0)
    return out

# ----------------------------------------------------------------- data sources
def fetch_rate():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US"
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    rows = [row for row in csv.reader(io.StringIO(r.text))][1:]
    rows = [(d, float(v)) for d, v in rows if v not in (".", "")]
    (d0, v0), (d1, v1) = rows[-2], rows[-1]
    return {"date": d1, "rate": v1, "prev_date": d0, "prev": v0, "change": round(v1 - v0, 2),
            "source": "Freddie Mac PMMS via FRED (MORTGAGE30US), weekly"}

def fetch_listings(city, days, sc, location=None):
    from homeharvest import scrape_property
    kw = dict(location=location or f"{city}, CA", listing_type="for_sale", past_days=days,
              property_type=sc.get("property_types") or None, exclude_pending=sc.get("exclude_pending", True),
              beds_min=sc.get("beds_min"), baths_min=sc.get("baths_min"), sqft_min=sc.get("sqft_min"))
    if not sc.get("include_missing_lot_size"): kw["lot_sqft_min"] = sc.get("lot_sqft_min")
    kw = {k: v for k, v in kw.items() if v is not None}
    df = scrape_property(**kw)
    return [] if df is None else df.to_dict("records")

def google_news(query, days):
    q = f"{query} when:{days}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    root = ET.fromstring(r.content); items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        src = it.find("source"); source = src.text if src is not None else ""
        if source and title.endswith(" - " + source): title = title[: -len(source) - 3]
        try: pub = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ)
        except Exception: pub = None
        items.append({"title": title, "link": it.findtext("link"), "source": source, "pub": pub})
    return items

def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:70]

def city_news(city, cfg):
    nc = cfg["news"]; cutoff = now_local() - dt.timedelta(days=nc["days"]); seen = set(); out = []
    for cat, tmpl in nc["queries"].items():
        try: items = google_news(tmpl.format(city=city), nc["days"])
        except Exception as e:
            log.warning("news %s/%s failed: %s", city, cat, e); continue
        for it in items:
            k = norm_title(it["title"])
            if k in seen or (it["pub"] and it["pub"] < cutoff): continue
            seen.add(k); it["cat"] = cat; out.append(it)
        time.sleep(1)
    out.sort(key=lambda x: x["pub"] or cutoff, reverse=True)
    # balance categories: round-robin so one topic doesn't crowd others
    by = {}
    for it in out: by.setdefault(it["cat"], []).append(it)
    bal = []
    while len(bal) < nc["max_per_city"] and any(by.values()):
        for cat in list(by):
            if by[cat] and len(bal) < nc["max_per_city"]: bal.append(by[cat].pop(0))
    bal.sort(key=lambda x: x["pub"] or cutoff, reverse=True)
    for it in bal: it["pub_str"] = it["pub"].strftime("%b %d") if it["pub"] else ""
    return bal

def embed_photo(url):
    if not url: return ""
    try:
        from PIL import Image
        big = re.sub(r"s\.jpg$", "od-w480_h360.jpg", url) if "rdcpix" in url else url
        r = requests.get(big, headers=UA, timeout=20)
        if r.status_code != 200: r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB"); im.thumbnail((480, 360))
        b = io.BytesIO(); im.save(b, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
    except Exception as e:
        log.info("photo embed failed (%s); using remote url", e); return url


# ----------------------------------------------------------------- storage
def db_open(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY, city TEXT, first_seen TEXT,
                   last_seen TEXT, price REAL, url TEXT, data TEXT, was_new INTEGER DEFAULT 1)""")
    con.execute("CREATE TABLE IF NOT EXISTS runs(ts TEXT, status TEXT, n_new INTEGER, report TEXT)")
    return con


# ----------------------------------------------------------------- build
def build_listing(rec, area, cfg, rate):
    lid = s(rec.get("property_id")) or s(rec.get("listing_id")) or s(rec.get("property_url"))
    price = num(rec.get("list_price"))
    if not price: return None
    full, half = num(rec.get("full_baths")) or 0, num(rec.get("half_baths")) or 0
    yb = num(rec.get("year_built"))
    mr = cfg["mello_roos"]
    mello = area["city"] in mr.get("likely_cities", []) or (yb and yb >= mr["year_built_threshold"])
    sqft, lot = num(rec.get("sqft")), num(rec.get("lot_sqft"))
    ld = s(rec.get("list_date"))[:10]
    return {
        "id": lid, "city": area["city"], "county": area["county"], "url": s(rec.get("property_url")),
        "address": s(rec.get("full_street_line")) or s(rec.get("street")),
        "city_line": f"{s(rec.get('city'))}, {s(rec.get('state'))} {s(rec.get('zip_code'))}",
        "photo": s(rec.get("primary_photo")), "price": price,
        "beds": int(num(rec.get("beds")) or 0), "full_baths": int(full), "half_baths": int(half),
        "baths": full + 0.5 * half, "sqft": sqft, "lot": lot, "year": int(yb) if yb else None,
        "hoa": num(rec.get("hoa_fee")) or 0.0, "dom": num(rec.get("days_on_mls")), "list_date": ld,
        "ppsf": round(price / sqft) if sqft else None, "tax_bill": num(rec.get("tax")),
        "est_value": num(rec.get("estimated_value")), "mello": bool(mello),
        "new_construction": bool(rec.get("new_construction")) if s(rec.get("new_construction")) else False,
        "status": s(rec.get("status")), "mls": s(rec.get("mls")), "mls_id": s(rec.get("mls_id")),
    }

def already_ran_today(con, today):
    return con.execute("SELECT COUNT(*) FROM runs WHERE status='ok' AND substr(ts,1,10)=?", (today,)).fetchone()[0] > 0

def gh_output(key, val):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a") as fh: fh.write(f"{key}={val}\n")

def run(cfg, args):
    t0 = now_local(); today = t0.strftime("%Y-%m-%d")
    (ROOT / "data").mkdir(exist_ok=True)
    con = db_open(ROOT / "data" / "scout.db")
    if args.scheduled:
        # GitHub cron fires at 13:00 and 14:00 UTC (covers PDT and PST); only the first one at/after 6 AM Pacific runs.
        if t0.hour < 6 or already_ran_today(con, today):
            log.info("scheduled guard: skipping (local hour %d, already ran today=%s)", t0.hour, already_ran_today(con, today))
            gh_output("ran", "false"); return None
    first_run = con.execute("SELECT COUNT(*) FROM runs WHERE status='ok'").fetchone()[0] == 0
    sc, fin = cfg["search"], cfg["finance"]
    errors = []

    try: rate_info = fetch_rate()
    except Exception as e:
        log.error("rate fetch failed: %s", e); errors.append(f"Mortgage rate fetch failed ({e}); using fallback.")
        rate_info = {"date": "n/a", "rate": fin.get("rate_override_pct") or 7.0, "prev": None, "change": None,
                     "prev_date": "", "source": "fallback (fetch failed)"}
    rate = fin.get("rate_override_pct") or rate_info["rate"]

    listings, area_stats = [], []
    initial_cut = (t0 - dt.timedelta(days=sc["initial_run_days"])).strftime("%Y-%m-%d")
    stale_cut = (t0 - dt.timedelta(days=cfg["report"].get("stale_fallback_days", 14))).strftime("%Y-%m-%d")
    seen_ids = set(); n_ok = 0
    for i, area in enumerate(cfg["areas"]):
        city = area["city"]; stale = False
        if i: time.sleep(sc.get("pause_between_cities_sec", 3))
        rows = []
        try:
            recs = fetch_listings(city, sc["fetch_days"], sc, area.get("location"))
            log.info("%s: %d listings fetched", city, len(recs)); n_ok += 1
            for rec in recs:
                L = build_listing(rec, area, cfg, rate)
                if not L or L["id"] in seen_ids: continue
                if s(rec.get("city")) and s(rec.get("city")).lower() != city.lower(): continue
                seen_ids.add(L["id"]); rows.append(L)
            for L in rows:
                prev = con.execute("SELECT first_seen, was_new FROM listings WHERE id=?", (L["id"],)).fetchone()
                if prev is None:
                    L["first_seen"] = today
                    L["is_new"] = (L["list_date"] >= initial_cut) if first_run else True
                else:
                    L["first_seen"] = prev[0]
                    L["is_new"] = prev[0] == today and bool(prev[1])  # same-day re-run keeps today's new ones
                L["initial"] = L["is_new"] and (first_run or con.execute(
                    "SELECT COUNT(*) FROM runs WHERE status='ok' AND substr(ts,1,10) < ?", (today,)).fetchone()[0] == 0)
                if not args.dry_run:
                    if prev is None:
                        con.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?)",
                                    (L["id"], city, today, today, L["price"], L["url"], json.dumps(L), int(L["is_new"])))
                    else:
                        con.execute("UPDATE listings SET last_seen=?, price=?, data=? WHERE id=?",
                                    (today, L["price"], json.dumps(L), L["id"]))
        except Exception as e:
            log.error("%s fetch failed: %s", city, e)
            # graceful fallback: last known listings for this city
            old = con.execute("SELECT data FROM listings WHERE city=? AND last_seen>=? ", (city, stale_cut)).fetchall()
            for (d,) in old:
                L = json.loads(d)
                if L["id"] in seen_ids: continue
                L.update(is_new=False, initial=False, stale=True); seen_ids.add(L["id"]); rows.append(L)
            stale = True
            errors.append(f"{city}: live fetch failed ({type(e).__name__}: {str(e)[:160]}). "
                          f"Showing {len(rows)} last-known listing(s) instead.")
        listings += rows
        prices = [L["price"] for L in rows]; ppsf = [L["ppsf"] for L in rows if L.get("ppsf")]
        lots = [L["lot"] for L in rows if L.get("lot")]
        area_stats.append({"area": area, "n_all": len(rows), "n_new": sum(bool(L.get("is_new")) for L in rows), "stale": stale,
                           "median_price": statistics.median(prices) if prices else None,
                           "median_ppsf": statistics.median(ppsf) if ppsf else None,
                           "median_lot": statistics.median(lots) if lots else None})

    for L in listings:
        county = cfg["counties"][L["county"]]
        L["fin"] = [dict(sc_, **monthly_cost(L["price"], sc_["down_pct"], sc_["type"], rate, fin, county, L.get("hoa") or 0))
                    for sc_ in fin["scenarios"]]
        L["myhome_amt"] = L["price"] * 0.035
        L.setdefault("stale", False)
        L["photo_src"] = embed_photo(L["photo"]) if (L["is_new"] and cfg["report"].get("embed_photos")) else L.get("photo", "")
    listings.sort(key=lambda L: (not L["is_new"], L["price"]))

    news = {}
    if cfg["news"].get("enabled"):
        for area in cfg["areas"]:
            try: news[area["city"]] = city_news(area["city"], cfg)
            except Exception as e:
                log.warning("news %s failed: %s", area["city"], e); news[area["city"]] = []

    status = "ok" if n_ok else "failed"
    if not n_ok: errors.insert(0, "All listing fetches failed - showing last-known data only. New-listing tracking was not updated.")
    ctx = dict(cfg=cfg, today=today, generated=t0.strftime("%a %b %d, %Y %I:%M %p PT"), rate=rate_info, used_rate=rate,
               listings=listings, n_new=sum(bool(L["is_new"]) for L in listings), first_run=first_run,
               area_stats=area_stats, news=news, errors=errors,
               js_data=json.dumps({"listings": [{"id": L["id"], "city": L["city"], "price": L["price"], "beds": L["beds"],
                                                 "baths": L["baths"], "lot": L.get("lot"), "is_new": bool(L["is_new"]),
                                                 "list_date": L.get("list_date", "")} for L in listings],
                                   "defaults": cfg["view_defaults"], "cities": [a["city"] for a in cfg["areas"]]}))
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = lambda v: "-" if v is None else f"${v:,.0f}"
    env.filters["intc"] = lambda v: "-" if v is None else f"{v:,.0f}"
    tpl = env.get_template("report.html.j2")
    html_out = tpl.render(email_mode=False, **ctx)
    rep_dir = ROOT / "reports"; rep_dir.mkdir(exist_ok=True)
    out = rep_dir / f"{today}.html"
    out.write_text(html_out, encoding="utf-8"); (rep_dir / "latest.html").write_text(html_out, encoding="utf-8")
    for f in (ROOT / "web").glob("*"):          # manifest + icons next to the report (for "Add to Home Screen")
        shutil.copy(f, rep_dir / f.name)
    log.info("report written: %s (%d new / %d total listings, %d errors)", out, ctx["n_new"], len(listings), len(errors))

    if not args.dry_run:
        con.execute("INSERT INTO runs VALUES(?,?,?,?)", (t0.isoformat(), status, ctx["n_new"], str(out))); con.commit()
    gh_output("ran", "true")

    if cfg.get("email", {}).get("enabled") and not args.no_email:
        for L in listings: L["photo_src"] = L.get("photo", "")
        try: send_email(cfg, tpl.render(email_mode=True, **ctx), out, ctx)
        except Exception as e: log.error("email failed: %s", e)
    return out


def send_email(cfg, body_html, attach_path, ctx):
    e = os.environ
    need = ["HOME_SCOUT_SMTP_USER", "HOME_SCOUT_SMTP_PASSWORD", "HOME_SCOUT_EMAIL_TO"]
    if any(not e.get(k) for k in need):
        log.warning("email enabled in config but env vars missing (%s); skipping", ", ".join(need)); return
    msg = EmailMessage()
    msg["Subject"] = f"{cfg['email']['subject_prefix']} {ctx['today']}: {ctx['n_new']} new listing(s), 30-yr {ctx['rate']['rate']}%"
    msg["From"] = e.get("HOME_SCOUT_EMAIL_FROM", e["HOME_SCOUT_SMTP_USER"])
    msg["To"] = e["HOME_SCOUT_EMAIL_TO"]
    msg.set_content("Your Home Scout digest is below (HTML) and attached.")
    msg.add_alternative(body_html, subtype="html")
    if cfg["email"].get("attach_html"):
        msg.add_attachment(attach_path.read_bytes(), maintype="text", subtype="html", filename=attach_path.name)
    with smtplib.SMTP(e.get("HOME_SCOUT_SMTP_HOST", "smtp.gmail.com"), int(e.get("HOME_SCOUT_SMTP_PORT", "587")), timeout=60) as smtp:
        smtp.starttls(); smtp.login(e["HOME_SCOUT_SMTP_USER"], e["HOME_SCOUT_SMTP_PASSWORD"]); smtp.send_message(msg)
    log.info("email sent to %s", e["HOME_SCOUT_EMAIL_TO"])


def main():
    ap = argparse.ArgumentParser(description="Home Scout daily digest")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="don't update the seen-listings database")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--scheduled", action="store_true", help="cron guard: run once per day, at/after 6 AM Pacific")
    args = ap.parse_args()
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / "home-scout.log"), logging.StreamHandler()])
    cfg = yaml.safe_load(open(args.config))
    try: run(cfg, args)
    except Exception:
        log.exception("run failed"); sys.exit(1)

if __name__ == "__main__":
    main()
