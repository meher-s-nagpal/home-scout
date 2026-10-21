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
