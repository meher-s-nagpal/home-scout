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
import hs_locations, hs_market, hs_news
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
def _rate_rows_fred():
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US", headers=UA, timeout=45)
    r.raise_for_status()
    rows = [row for row in csv.reader(io.StringIO(r.text))][1:]
    return [(r[0], float(r[1])) for r in rows if len(r) == 2 and r[1] not in (".", "")], "Freddie Mac PMMS via FRED (MORTGAGE30US), weekly"

def _rate_rows_freddie():
    r = requests.get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv", headers=UA, timeout=45)
    r.raise_for_status(); out = []
    for row in list(csv.DictReader(io.StringIO(r.text))):
        try:
            m, d, y = row["date"].split("/"); out.append((f"{y}-{int(m):02d}-{int(d):02d}", float(row["pmms30"])))
        except (ValueError, KeyError): pass
    return out, "Freddie Mac PMMS (freddiemac.com), weekly"

def fetch_rate():
    """Latest 30-yr fixed: Freddie Mac PMMS CSV, then FRED (each tried twice)."""
    last = None
    for fn in (_rate_rows_freddie, _rate_rows_fred):   # FRED times out from GitHub runners
        for attempt in range(2):
            try:
                rows, src = fn()
                (d0, v0), (d1, v1) = rows[-2], rows[-1]
                return {"date": d1, "rate": v1, "prev_date": d0, "prev": v0, "change": round(v1 - v0, 2), "source": src}
            except Exception as e:
                last = e; log.warning("rate source %s failed (attempt %d): %s", fn.__name__, attempt + 1, e); time.sleep(3)
    raise RuntimeError(f"all rate sources failed: {last}")


def fetch_listings(location, sc, listing_type="for_sale", days=None):
    """One HomeHarvest call. Lot-size filter is applied in Python so new builds on small lots are still seen."""
    from homeharvest import scrape_property
    kw = dict(location=location, listing_type=listing_type, past_days=days,
              property_type=sc.get("property_types") or None, beds_min=sc.get("beds_min"),
              baths_min=sc.get("baths_min"), sqft_min=sc.get("sqft_min"))
    if listing_type == "for_sale": kw["exclude_pending"] = sc.get("exclude_pending", True)
    kw = {k: v for k, v in kw.items() if v is not None}
    df = scrape_property(**kw)
    return [] if df is None else df.to_dict("records")


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
def hires(u):
    """Realtor.com (rdcpix) photos: ask for the 1024x768 variant instead of 480x360 (capped by w=1080)."""
    if not u or "rdcpix.com" not in u: return u
    u = re.sub(r"od-w480_h360", "od-w1024_h768", u)
    return re.sub(r"(-m\d+)s\.jpg$", r"\1od-w1024_h768.jpg", u)

def photo_list(rec, cfg):
    urls = [s(rec.get("primary_photo"))] + [x.strip() for x in s(rec.get("alt_photos")).split(",")]
    out, seen = [], set()
    for u in urls:
        if not u.startswith("http"): continue
        k = re.sub(r"od-w\d+_h\d+.*$|s\.jpg$", "", u)
        if k in seen: continue
        seen.add(k); out.append(hires(u))
    return out[: cfg["report"].get("max_photos", 15)]

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
        "photo": hires(s(rec.get("primary_photo"))), "photos": photo_list(rec, cfg), "price": price,
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

def county_info(cfg, name):
    c = cfg["counties"].get(name)
    if c: return dict(c, name=name, unverified=False)
    return dict(cfg["counties"]["_default"], name=name or "Unknown", unverified=True)

def lot_ok(L, sc):
    if L.get("lot") is None: return bool(sc.get("include_missing_lot_size"))
    return L["lot"] >= (sc.get("lot_sqft_min") or 0)

def city_matches(rec, area):
    if not area.get("match_city", True): return True
    c = s(rec.get("city"))
    return not c or c.lower() == area["city"].lower()


def run(cfg, args):
    t0 = now_local(); today = t0.strftime("%Y-%m-%d"); year = t0.year
    (ROOT / "data").mkdir(exist_ok=True)
    con = db_open(ROOT / "data" / "scout.db"); hs_market.init(con)
    if args.scheduled and not (args.add_location or args.remove_location):
        # GitHub cron fires at 13:00 and 14:00 UTC (covers PDT and PST); only the first one at/after 6 AM Pacific runs.
        if t0.hour < 6 or already_ran_today(con, today):
            log.info("scheduled guard: skipping (local hour %d, already ran today=%s)", t0.hour, already_ran_today(con, today))
            gh_output("ran", "false"); return None

    loc_path = ROOT / "locations.yaml"
    loc_msgs = []
    if args.remove_location: loc_msgs += hs_locations.remove(loc_path, args.remove_location)
    if args.add_location: loc_msgs += hs_locations.add(loc_path, args.add_location, cfg.get("regions"))
    for m in loc_msgs: log.info("locations: %s", m)
    if args.locations_only: return None
    areas = hs_locations.load(loc_path) or cfg.get("areas", [])
    for a in areas:
        if not a.get("region"): a["region"] = hs_locations.assign_region(a, cfg.get("regions"))

    first_run = con.execute("SELECT COUNT(*) FROM runs WHERE status='ok'").fetchone()[0] == 0
    had_runs_before_today = con.execute("SELECT COUNT(*) FROM runs WHERE status='ok' AND substr(ts,1,10) < ?",
                                        (today,)).fetchone()[0] > 0
    sc, fin = cfg["search"], cfg["finance"]
    errors = [f"Locations: {m}" for m in loc_msgs if m.startswith(("could not", "warning"))]
    notices = [m for m in loc_msgs if not m.startswith(("could not", "warning"))]

    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    try:
        rate_info = fetch_rate()
        con.execute("INSERT OR REPLACE INTO meta VALUES('rate', ?)", (json.dumps(rate_info),))
    except Exception as e:
        log.error("rate fetch failed: %s", e)
        cached = con.execute("SELECT v FROM meta WHERE k='rate'").fetchone()
        if cached:
            rate_info = json.loads(cached[0]); rate_info["source"] += " (cached - live fetch failed)"
            errors.append(f"Mortgage rate fetch failed; using last known rate {rate_info['rate']}% (week of {rate_info['date']}).")
        else:
            rate_info = {"date": "n/a", "rate": fin.get("rate_override_pct") or 7.0, "prev": None, "change": None,
                         "prev_date": "", "source": "fallback (fetch failed)"}
            errors.append(f"Mortgage rate fetch failed ({e}); using fallback {rate_info['rate']}%.")
    rate = fin.get("rate_override_pct") or rate_info["rate"]

    new_cut = (t0 - dt.timedelta(days=sc["initial_run_days"] if first_run else sc.get("new_window_days", 7))).strftime("%Y-%m-%d")
    card_cut = (t0 - dt.timedelta(days=sc.get("card_days", 30))).strftime("%Y-%m-%d")
    stale_cut = (t0 - dt.timedelta(days=cfg["report"].get("stale_fallback_days", 14))).strftime("%Y-%m-%d")
    nb_cfg = cfg.get("developments", {}).get("new_builds", {})
    listings, area_stats, new_builds = [], [], {}
    seen_ids = set(); n_ok = 0

    for i, area in enumerate(areas):
        city = area["city"]; loc = area.get("location") or f"{city}, CA"; stale = False
        area["county_info"] = county_info(cfg, area.get("county"))
        if i: time.sleep(sc.get("pause_between_cities_sec", 3))
        rows, active = [], []
        try:
            recs = fetch_listings(loc, sc, "for_sale", sc.get("fetch_days"))
            log.info("%s: %d active listings fetched", city, len(recs)); n_ok += 1
            nb = []
            for rec in recs:
                if not city_matches(rec, area): continue
                L = build_listing(rec, area, cfg, rate)
                if not L or L["id"] in seen_ids: continue
                seen_ids.add(L["id"])
                yb = L.get("year") or 0
                if L["new_construction"] or yb >= year - nb_cfg.get("year_built_min_offset", 1):
                    nb.append(dict(L, builder=s(rec.get("builder_name"))))
                if lot_ok(L, sc): active.append(L)
            nb.sort(key=lambda x: x.get("list_date") or "", reverse=True)
            new_builds[city] = {"n": len(nb), "items": nb[: nb_cfg.get("max_per_city", 8)]}
            for L in active:
                prev = con.execute("SELECT first_seen, was_new FROM listings WHERE id=?", (L["id"],)).fetchone()
                if prev is None:
                    L["first_seen"] = today
                    L["is_new"] = (not L["list_date"]) or L["list_date"] >= new_cut
                else:
                    L["first_seen"] = prev[0]
                    L["is_new"] = prev[0] == today and bool(prev[1])   # same-day re-run keeps today's new ones
                L["initial"] = L["is_new"] and not had_runs_before_today
                if not args.dry_run:
                    if prev is None:
                        con.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?)",
                                    (L["id"], city, today, today, L["price"], L["url"], json.dumps(L), int(L["is_new"])))
                    else:
                        con.execute("UPDATE listings SET last_seen=?, price=?, data=? WHERE id=?",
                                    (today, L["price"], json.dumps(L), L["id"]))
            rows = [L for L in active if L["is_new"] or (L["list_date"] or "") >= card_cut]
        except Exception as e:
            log.error("%s fetch failed: %s", city, e); stale = True
            for (d,) in con.execute("SELECT data FROM listings WHERE city=? AND last_seen>=?", (city, stale_cut)).fetchall():
                L = json.loads(d)
                if L["id"] in seen_ids: continue
                L.update(is_new=False, initial=False, stale=True); seen_ids.add(L["id"]); rows.append(L)
            active = rows
            errors.append(f"{city}: live fetch failed ({type(e).__name__}: {str(e)[:160]}). Showing {len(rows)} last-known listing(s).")

        # recently sold comps
        sold = None
        if not stale and sc.get("sold_days"):
            try:
                srecs = [r for r in fetch_listings(loc, sc, "sold", sc["sold_days"]) if city_matches(r, area)]
                sold = hs_market.sold_summary(srecs, today, sc.get("lot_sqft_min") if not sc.get("include_missing_lot_size") else None)
                sold["all90"] = hs_market.sold_summary(srecs, today)[90]
                log.info("%s: %d sold records (%d days)", city, len(srecs), sc["sold_days"])
            except Exception as e:
                log.warning("%s sold fetch failed: %s", city, e)

        listings += rows
        st = {"area": area, "stale": stale, "n_cards": len(rows), "n_new": sum(bool(L.get("is_new")) for L in rows),
              "n_active": len(active), "median_price": hs_market.med([L["price"] for L in active]),
              "median_ppsf": hs_market.med([L.get("ppsf") for L in active]),
              "median_dom": hs_market.med([L.get("dom") for L in active]),
              "median_lot": hs_market.med([L.get("lot") for L in active]), "sold": sold}
        if not stale and not args.dry_run:
            hs_market.record(con, today, city, {"n_active": st["n_active"], "median_price": st["median_price"],
                             "median_ppsf": st["median_ppsf"], "median_dom": st["median_dom"], "n_new": st["n_new"],
                             "sold_n": sold[30]["n"] if sold else None, "sold_median": sold[30]["median"] if sold else None,
                             "sold_ppsf": sold[30]["ppsf"] if sold else None})
        area_stats.append(st)

    # pricing trends from our own history
    for st in area_stats:
        c = st["area"]["city"]
        hist = hs_market.history(con, c, 120)
        st["hist_start"] = hs_market.first_date(con, c); st["hist_days"] = len(hist)
        st["spark_price"] = hs_market.sparkline([h["median_price"] for h in hist])
        st["spark_inv"] = hs_market.sparkline([h["n_active"] for h in hist])
        st["chg"] = {m: {d: hs_market.change(con, c, m, today, d) for d in (30, 90)}
                     for m in ("median_price", "median_ppsf", "n_active", "median_dom")}

    for L in listings:
        county = county_info(cfg, L.get("county"))
        L["county_info"] = county
        L["fin"] = [dict(sc_, **monthly_cost(L["price"], sc_["down_pct"], sc_["type"], rate, fin, county, L.get("hoa") or 0))
                    for sc_ in fin["scenarios"]]
        L["myhome_amt"] = L["price"] * 0.035
        L.setdefault("stale", False)
        L["photo_src"] = embed_photo(L["photo"]) if (L["is_new"] and cfg["report"].get("embed_photos")) else L.get("photo", "")
        L["photos"] = L.get("photos") or ([L["photo"]] if L.get("photo") else [])
        # compact photo encoding for the page: common prefix/suffix + the varying middle parts (JS rebuilds the <img>s)
        ph = L["photos"]
        pre = os.path.commonprefix(ph) if len(ph) > 1 else ""
        suf = os.path.commonprefix([u[len(pre):][::-1] for u in ph])[::-1] if len(ph) > 1 else ""
        L["ph_pre"], L["ph_suf"] = pre, suf
        L["ph_mid"] = "|".join(u[len(pre): len(u) - len(suf)] for u in ph[1:]) if len(ph) > 1 else ""
        if L["photo_src"] and L["photo_src"].startswith("data:"): L["photos"] = [L["photo_src"]] + L["photos"][1:]
        L["search"] = " ".join([L.get("address", ""), L.get("city_line", ""), L.get("city", "")]).lower()
    listings.sort(key=lambda L: (not L["is_new"], L["price"]))

    # news: area headlines, developments, first-time buyer watch
    news, devs, watch = {}, {}, []
    nc = cfg["news"]; pause = nc.get("pause_sec", 0.6)
    for area in areas:
        nm = area.get("news_name") or area["city"]
        excl = (nc.get("global_exclude") or []) + (area.get("news_exclude") or [])
        if nc.get("enabled"):
            items = hs_news.collect([(k, hs_news.with_excludes(q.format(city=nm), excl)) for k, q in nc["queries"].items()],
                                    nc["days"], pause)
            news[area["city"]] = hs_news.balance(items, nc["max_per_city"])
        dc = cfg.get("developments", {})
        if dc.get("enabled"):
            items = hs_news.collect([(k, hs_news.with_excludes(q.format(city=nm), excl)) for k, q in dc["queries"]], dc["days"], pause)
            region = [r.lower() for r in dc.get("region_terms", [])] + [f"{(area.get('county') or '').lower()} county"]
            devs[area["city"]] = hs_news.dev_filter(items, nm, [k.lower() for k in dc["keywords"]],
                                                    [x.lower() for x in dc.get("exclude", [])] +
                                                    [x.lower() for x in (area.get("news_exclude") or [])], region)[: dc["max_per_city"]]
    bw = cfg.get("buyer_watch", {})
    if bw.get("enabled"):
        items = hs_news.collect([tuple(q) for q in bw["queries"]], bw["days"], pause)
        watch = [x for x in hs_news.score(items, bw["weights"], bw.get("negative", {})) if x["score"] > 3][: bw["show"]]

    modules = []
    nm_cfg = cfg.get("news_modules", {})
    if nm_cfg.get("enabled"):
        hs_news.cache_init(con)
        for r in cfg.get("regions", []):
            ra = [a for a in areas if a.get("region") == r["name"]]
            if not ra: continue
            try:
                cards = hs_news.region_module(con, r, ra, nm_cfg, pause, nm_cfg.get("page_timeout_sec", 8))
            except Exception as e:
                log.warning("news module %s failed: %s", r["name"], e); cards = []
            modules.append({"name": r["name"], "title": r.get("title") or f"{r['name']} News", "cards": cards,
                            "cities": [a["city"] for a in ra]})
            log.info("news module %s: %d cards (%d with publisher summaries)", r["name"], len(cards),
                     sum(c["summary_src"] == "publisher" for c in cards))
        con.commit()

    status = "ok" if n_ok else "failed"
    if areas and not n_ok:
        errors.insert(0, "All listing fetches failed - showing last-known data only. New-listing tracking was not updated.")
    repo = os.environ.get("GITHUB_REPOSITORY")
    manage_url = f"https://github.com/{repo}/actions/workflows/scout.yml" if repo else None
    ctx = dict(cfg=cfg, areas=areas, today=today, generated=t0.strftime("%a %b %d, %Y %I:%M %p PT"), rate=rate_info,
               used_rate=rate, listings=listings, n_new=sum(bool(L["is_new"]) for L in listings), first_run=first_run,
               area_stats=area_stats, news=news, devs=devs, modules=modules, new_builds=new_builds, watch=watch, errors=errors,
               notices=notices, manage_url=manage_url,
               js_data=json.dumps({"listings": [{"id": L["id"], "city": L["city"], "price": L["price"], "beds": L["beds"],
                                                 "baths": L["baths"], "lot": L.get("lot"), "is_new": bool(L["is_new"]),
                                                 "list_date": L.get("list_date", ""), "q": L["search"],
                                                 # snapshot fields for Favorites (photos are read from the card)
                                                 "addr": L.get("address", ""), "cl": L.get("city_line", ""), "url": L.get("url", ""),
                                                 "sqft": L.get("sqft"), "fb": L.get("full_baths"), "hb": L.get("half_baths"),
                                                 "year": L.get("year"), "hoa": L.get("hoa"), "county": L.get("county"),
                                                 "fin": [[f["label"], round(f["down"]), round(f["loan"]), round(f["total"]), bool(f["over_limit"])]
                                                         for f in L["fin"]]} for L in listings],
                                   "today": today, "rate": rate,
                                   "defaults": cfg["view_defaults"]}))
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = lambda v: "-" if v is None else f"${v:,.0f}"
    env.filters["intc"] = lambda v: "-" if v is None else f"{v:,.0f}"
    env.filters["pct"] = lambda v: "-" if v is None else f"{v:+.1f}%"
    tpl = env.get_template("report.html.j2")
    html_out = tpl.render(email_mode=False, **ctx)
    rep_dir = ROOT / "reports"; rep_dir.mkdir(exist_ok=True)
    out = rep_dir / f"{today}.html"
    out.write_text(html_out, encoding="utf-8"); (rep_dir / "latest.html").write_text(html_out, encoding="utf-8")
    for f in (ROOT / "web").glob("*"):
        shutil.copy(f, rep_dir / f.name)
    log.info("report written: %s (%d new / %d cards, %d errors, %d KB)", out, ctx["n_new"], len(listings), len(errors),
             len(html_out) // 1024)

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
    ap.add_argument("--dry-run", action="store_true", help="don't update the database")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--scheduled", action="store_true", help="cron guard: run once per day, at/after 6 AM Pacific")
    ap.add_argument("--add-location", action="append", default=[], metavar="LOC",
                    help="add area(s): 'Brentwood, CA' or zip; comma-separated OK; repeatable")
    ap.add_argument("--remove-location", action="append", default=[], metavar="LOC", help="remove area(s) by name or zip")
    ap.add_argument("--locations-only", action="store_true", help="only update locations.yaml, don't run the scout")
    args = ap.parse_args()
    # GitHub workflow inputs arrive via env (avoids shell-quoting issues)
    if os.environ.get("HS_ADD_LOCATIONS", "").strip(): args.add_location.append(os.environ["HS_ADD_LOCATIONS"])
    if os.environ.get("HS_REMOVE_LOCATIONS", "").strip(): args.remove_location.append(os.environ["HS_REMOVE_LOCATIONS"])
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / "home-scout.log"), logging.StreamHandler()])
    cfg = yaml.safe_load(open(args.config))
    try: run(cfg, args)
    except Exception:
        log.exception("run failed"); sys.exit(1)

if __name__ == "__main__":
    main()
