"""Google News RSS collection, dedupe and rule-based (keyword) ranking. No AI."""
import datetime as dt, email.utils, logging, re, time, urllib.parse, xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
import requests

log = logging.getLogger("home-scout")
TZ = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) HomeScout/1.0 (personal use)"}
OTHER_STATES = ["texas", "florida", "arizona", "nevada", "colorado", "georgia", "ohio", "new york", "new jersey",
                "illinois", "washington state", "oregon", "utah", "idaho", "north carolina", "tennessee", "hawaii",
                "kapolei", "michigan", "virginia", "massachusetts", "pennsylvania", "canada", "uk ", "australia"]

def google_news(query, days):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title = it.findtext("title") or ""
        src = it.find("source"); source = src.text if src is not None else ""
        if source and title.endswith(" - " + source): title = title[: -len(source) - 3]
        try: pub = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ)
        except Exception: pub = None
        items.append({"title": title, "link": it.findtext("link"), "source": source, "pub": pub})
    return items

def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:70]

def collect(queries, days, pause=0.6):
    """queries: list of (category, query). Returns deduped items newest first."""
    cutoff = dt.datetime.now(TZ) - dt.timedelta(days=days); seen = set(); out = []
    for cat, q in queries:
        try: items = google_news(q, days)
        except Exception as e:
            log.warning("news query failed (%s): %s", q, e); continue
        for it in items:
            k = norm(it["title"])
            if not k or k in seen or (it["pub"] and it["pub"] < cutoff): continue
            seen.add(k); it["cat"] = cat; out.append(it)
        time.sleep(pause)
    out.sort(key=lambda x: x["pub"] or cutoff, reverse=True)
    for it in out: it["pub_str"] = it["pub"].strftime("%b %d") if it["pub"] else ""
    return out

def balance(items, n):
    """Round-robin across categories so one topic doesn't crowd the others."""
    by = {}
    for it in items: by.setdefault(it["cat"], []).append(it)
    res = []
    while len(res) < n and any(by.values()):
        for c in list(by):
            if by[c] and len(res) < n: res.append(by[c].pop(0))
    return sorted(res, key=lambda x: x["pub"] or dt.datetime.min.replace(tzinfo=TZ), reverse=True)

def mentions_other_state(title):
    t = title.lower()
    return any(s in t for s in OTHER_STATES) and "california" not in t

def dev_filter(items, city, keywords, exclude, region_terms):
    """Keep development headlines: >=1 development keyword AND (city or a regional term in the title), no excluded words."""
    out = []
    for it in items:
        t = f" {it['title'].lower()} "
        if mentions_other_state(t) or any(x in t for x in exclude): continue
        kws = [k for k in keywords if k in t]
        has_city = city.lower() in t
        if kws and (has_city or any(r in t for r in region_terms)):
            it["has_city"] = has_city; it["kw"] = len(kws); out.append(it)
    out.sort(key=lambda x: (not x["has_city"], -x["kw"], -(x["pub"].timestamp() if x["pub"] else 0)))
    return out

def with_excludes(query, words):
    return query + "".join(f' -"{w}"' if " " in w else f" -{w}" for w in words or [])

def score(items, weights, negative, recent_days=3):
    now = dt.datetime.now(TZ)
    for it in items:
        t = f" {it['title'].lower()} "
        s = sum(w for k, w in weights.items() if k in t)
        s -= sum(w for k, w in negative.items() if k in t)
        if mentions_other_state(t): s -= 4
        if it["pub"] and (now - it["pub"]).days < recent_days: s += 2
        it["score"] = s
    return sorted(items, key=lambda x: (-x["score"], -(x["pub"].timestamp() if x["pub"] else 0)))


# ------------------------------------------------------------------ regional housing-news modules
import html as _html, json as _json

PAGE_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36 "
                         "HomeScout/1.0 (personal news digest)"}
META_RES = [re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|twitter:description|description)["\'][^>]*?'
                       r'content=["\']([^"\']{20,})', re.I),
            re.compile(r'<meta[^>]+content=["\']([^"\']{20,})["\'][^>]*?(?:property|name)=["\']'
                       r'(?:og:description|twitter:description|description)', re.I)]

def cache_init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS article_cache(link TEXT PRIMARY KEY, url TEXT, summary TEXT,
                   source TEXT, fetched TEXT)""")

def resolve_and_summarize(con, item, timeout=8):
    """Publisher URL (decoded from the Google News link) + og:description. Cached in SQLite (fetched once)."""
    row = con.execute("SELECT url, summary, source FROM article_cache WHERE link=?", (item["link"],)).fetchone()
    if row: return {"url": row[0], "summary": row[1], "summary_src": row[2]}
    url, summary, src = item["link"], "", "none"
    try:
        from googlenewsdecoder import gnewsdecoder
        d = gnewsdecoder(item["link"], interval=0.5)
        if d.get("status") or d.get("success"):
            url = d.get("decoded_url") or url
    except Exception as e:
        log.info("gnews decode failed: %s", e)
    if "news.google.com" not in url:
        try:
            r = requests.get(url, headers=PAGE_UA, timeout=timeout, stream=True)
            body = r.raw.read(400_000, decode_content=True).decode(r.encoding or "utf-8", "ignore") if r.ok else ""
            for rx in META_RES:
                m = rx.search(body)
                if m:
                    summary = _html.unescape(m.group(1)).strip(); src = "publisher"; break
        except Exception as e:
            log.info("article fetch failed (%s): %s", url[:80], e)
    if summary and norm(summary)[:50] == norm(item["title"])[:50]: summary, src = "", "none"  # just the title again
    con.execute("INSERT OR REPLACE INTO article_cache VALUES(?,?,?,?,date('now'))", (item["link"], url, summary, src))
    return {"url": url, "summary": summary, "summary_src": src}

def housing_score(text, mod):
    t = f" {text.lower()} "
    if any(x in t for x in mod["exclude"]): return None
    if mentions_other_state(t): return None
    if not any(k in t for k in mod["core"]): return None
    return sum(w for k, w in mod["weights"].items() if k in t)

def is_local(it, text, local_terms, mod, outlets=()):
    from urllib.parse import urlparse
    u = urlparse(it.get("url") or ""); host = u.netloc.lower(); full = host + u.path.lower()
    if any(host.endswith(t) for t in mod.get("blocked_tlds", [])): return False
    if any(b in text for b in mod.get("blocked_text", [])): return False
    if "patch.com" in host and "/california/" not in u.path: return False
    if any(o in host for o in outlets): return True
    if not any(t in text for t in local_terms): return False
    if any(d in full for d in mod.get("local_domains", [])): return True
    if any(c in text for c in mod.get("context_terms", [])): return True
    return sum(1 for t in set(local_terms) if t in text) >= 2

def region_module(con, region, areas, mod, pause=0.6, timeout=8):
    """region: dict(name, terms); areas: locations in this region; mod: config news_modules."""
    names = [a.get("news_name") or a["city"] for a in areas]
    excludes = sorted({x.lower() for a in areas for x in (a.get("news_exclude") or [])})
    queries = []
    for a in areas:
        nm = a.get("news_name") or a["city"]
        q = with_excludes(mod["city_query"].format(city=nm), (a.get("news_exclude") or []) + mod.get("query_exclude", []))
        queries.append((nm, q))
    for term in region.get("terms", []):
        queries.append((term, with_excludes(mod["city_query"].format(city=term), mod.get("query_exclude", []))))
    for q in region.get("site_queries", []):
        queries.append(("site", q))
    items = collect(queries, mod["days"], pause)
    local_terms = [n.lower() for n in names] + [t.lower() for t in region.get("terms", [])]
    excl = mod["exclude"] + excludes
    cands = []
    blocked_src = [b.lower() for b in mod.get("blocked_sources", [])]
    for it in items:
        if any(b in (it["source"] or "").lower() for b in blocked_src): continue
        tl = f" {it['title'].lower()} "
        if any(b in tl for b in mod.get("blocked_text", [])): continue
        s = housing_score(it["title"], dict(mod, exclude=excl))
        if s is None: continue
        it["score"] = s; cands.append(it)
    outlets = [o.lower() for o in region.get("local_outlets", [])]
    cands.sort(key=lambda x: (not (any(t in x["title"].lower() for t in local_terms) or x["cat"] == "site"), -x["score"]))
    out = []
    now = dt.datetime.now(TZ)
    for it in cands[: mod.get("fetch_top", 30)]:
        info = resolve_and_summarize(con, it, timeout)
        it.update(info)
        text = f" {it['title']} {it['summary']} ".lower()
        if not is_local(it, text, local_terms, mod, region.get("local_outlets", [])): continue          # must be about this region
        if any(x in text for x in excl): continue
        s2 = housing_score(text, dict(mod, exclude=excl))
        if s2 is None: continue
        age = (now - it["pub"]).days if it["pub"] else 14
        it["rank"] = s2 + (3 if age < 3 else 1 if age < 7 else 0) + (2 if any(t in it["title"].lower() for t in local_terms) else 0)
        if it["rank"] >= mod.get("min_rank", 0): out.append(it)
    out.sort(key=lambda x: (-x["rank"], -(x["pub"].timestamp() if x["pub"] else 0)))
    out = out[: mod.get("max_cards", 12)]
    for it in out:
        if not it["summary"]:
            it["summary_src"] = "rss"; it["summary"] = ""   # Google News RSS description is only the headline
        if len(it["summary"]) > 260: it["summary"] = it["summary"][:257].rsplit(" ", 1)[0] + "…"
    return out
