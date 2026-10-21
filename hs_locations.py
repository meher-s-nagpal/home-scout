"""Locations file (locations.yaml): load / add / remove, geocoding (OpenStreetMap Nominatim) and
no-traffic drive times (OSRM public router). Plain code, no keys."""
import re, time, logging
from pathlib import Path
import requests, yaml

log = logging.getLogger("home-scout")
UA = {"User-Agent": "HomeScout/1.0 (personal home-search digest)"}
SAN_JOSE = (37.3361, -121.8906)   # downtown San Jose (lat, lon)
MODESTO = (37.6391, -120.9969)    # downtown Modesto
ZIP_RE = re.compile(r"^\d{5}$")
STATE_RE = re.compile(r"^([A-Za-z]{2}|california)$", re.I)


def load(path):
    p = Path(path)
    if not p.exists(): return []
    return (yaml.safe_load(p.read_text()) or {}).get("areas", [])

def save(path, areas):
    hdr = ("# Areas Home Scout searches. Edit by hand, or use:\n"
           "#   python scout.py --add-location 'Brentwood, CA' --remove-location 'Lathrop'\n"
           "#   (or the GitHub Actions 'Run workflow' form: add_locations / remove_locations)\n"
           "# Fields: city (label), county (for loan limits), location (search string; zip or 'City, CA'),\n"
           "#   match_city (keep only results whose city equals the label), news_name (name used in news queries),\n"
           "#   drive_min (no-traffic minutes to downtown San Jose / Modesto via OSRM), lat/lon.\n")
    Path(path).write_text(hdr + yaml.safe_dump({"areas": areas}, sort_keys=False, allow_unicode=True))

def parse_values(values):
    """'Brentwood, CA, 95377; Ripon' -> ['Brentwood, CA', '95377', 'Ripon']  (a 2-letter state joins the token before it)."""
    out = []
    for v in values or []:
        for t in re.split(r"[,;\n]+", v or ""):
            t = t.strip()
            if not t: continue
            if STATE_RE.match(t) and out and not ZIP_RE.match(out[-1]) and not re.search(r",\s*[A-Za-z]{2}$", out[-1]):
                out[-1] = f"{out[-1]}, {t.upper()}"
            else:
                out.append(t)
    return out

def _get(url, params):
    r = requests.get(url, params=params, headers=UA, timeout=30); r.raise_for_status(); time.sleep(1.1)  # Nominatim: <=1 req/s
    return r.json()

def geocode(q):
    base = "https://nominatim.openstreetmap.org"
    if ZIP_RE.match(q):
        res = _get(f"{base}/search", {"postalcode": q, "country": "us", "format": "jsonv2", "addressdetails": 1, "limit": 1})
        if not res: raise ValueError(f"zip {q} not found")
        g = res[0]
        rev = _get(f"{base}/reverse", {"lat": g["lat"], "lon": g["lon"], "format": "jsonv2", "zoom": 12, "addressdetails": 1})
        ra = rev.get("address", {})
        place = ra.get("city") or ra.get("town") or ra.get("village") or ra.get("hamlet") or _zip_city(q)
        addr = g.get("address", {})
        return {"city": f"{q} ({place})" if place else q, "location": q, "match_city": False, "news_name": place or q,
                "county": (addr.get("county") or ra.get("county") or "").replace(" County", ""),
                "state": addr.get("state") or ra.get("state"), "lat": float(g["lat"]), "lon": float(g["lon"])}
    name = re.sub(r",\s*(CA|California)$", "", q, flags=re.I).strip()
    res = _get(f"{base}/search", {"q": f"{name}, California, USA", "format": "jsonv2", "addressdetails": 1, "limit": 1, "countrycodes": "us"})
    if not res: raise ValueError(f"place '{q}' not found")
    g = res[0]; a = g.get("address", {})
    label = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or name.title()
    return {"city": label, "location": f"{label}, CA", "match_city": True, "news_name": label,
            "county": (a.get("county") or "").replace(" County", ""), "state": a.get("state"),
            "lat": float(g["lat"]), "lon": float(g["lon"])}

def _zip_city(z):
    """Most common city among current listings in a zip (Nominatim often has no city for zip centroids)."""
    try:
        from collections import Counter
        from homeharvest import scrape_property
        df = scrape_property(location=z, listing_type="for_sale", limit=100)
        c = Counter(str(x) for x in df["city"].dropna()) if df is not None and len(df) else None
        return c.most_common(1)[0][0] if c else ""
    except Exception:
        return ""

def drive_minutes(lat, lon):
    url = (f"https://router.project-osrm.org/table/v1/driving/{lon},{lat};{SAN_JOSE[1]},{SAN_JOSE[0]};"
           f"{MODESTO[1]},{MODESTO[0]}?sources=0&annotations=duration")
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    d = r.json()["durations"][0]
    return {"san_jose": round(d[1] / 60), "modesto": round(d[2] / 60)}

def _key(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r",\s*(ca|california)$", "", s.strip().lower()))

def find(areas, value):
    k = _key(value)
    for a in areas:
        keys = {_key(a.get("city", "")), _key(a.get("location", "")), _key(a.get("news_name", "") or ""),
                _key(re.sub(r"\s*\(.*\)", "", a.get("city", "")))}
        if k in keys: return a
    return None

def add(path, values):
    areas = load(path); msgs = []
    for v in parse_values(values):
        if find(areas, v): msgs.append(f"'{v}' is already in locations"); continue
        try:
            g = geocode(v)
            if g.get("state") and g["state"] != "California":
                msgs.append(f"warning: '{v}' resolved to {g['state']} (loan limits/CalHFA data are California-only)")
            g["drive_min"] = drive_minutes(g["lat"], g["lon"])
            g.pop("state", None)
            area = {k: g[k] for k in ("city", "county", "location", "match_city", "news_name", "drive_min", "lat", "lon")}
            area["added"] = time.strftime("%Y-%m-%d")
            areas.append(area)
            msgs.append(f"added {area['city']} ({area['county']} County): {area['drive_min']['san_jose']} min to San Jose, "
                        f"{area['drive_min']['modesto']} min to Modesto (no traffic)")
        except Exception as e:
            msgs.append(f"could not add '{v}': {e}")
    save(path, areas)
    return msgs

def remove(path, values):
    areas = load(path); msgs = []
    for v in parse_values(values):
        a = find(areas, v)
        if a: areas.remove(a); msgs.append(f"removed {a['city']}")
        else: msgs.append(f"'{v}' not found in locations")
    save(path, areas)
    return msgs
