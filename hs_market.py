"""Area pricing history (SQLite) + sparkline SVG + sold comps summary."""
import datetime as dt, statistics

def init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS area_history(date TEXT, city TEXT, n_active INTEGER, median_price REAL,
                   median_ppsf REAL, median_dom REAL, n_new INTEGER, sold_n INTEGER, sold_median REAL, sold_ppsf REAL,
                   PRIMARY KEY(date, city))""")

def med(xs):
    xs = [x for x in xs if x]
    return statistics.median(xs) if xs else None

def record(con, date, city, row):
    con.execute("INSERT OR REPLACE INTO area_history VALUES(?,?,?,?,?,?,?,?,?,?)",
                (date, city, row.get("n_active"), row.get("median_price"), row.get("median_ppsf"), row.get("median_dom"),
                 row.get("n_new"), row.get("sold_n"), row.get("sold_median"), row.get("sold_ppsf")))

def history(con, city, days=120):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    cur = con.execute("SELECT date, n_active, median_price, median_ppsf, median_dom FROM area_history "
                      "WHERE city=? AND date>=? ORDER BY date", (city, since))
    return [dict(zip(("date", "n_active", "median_price", "median_ppsf", "median_dom"), r)) for r in cur]

def change(con, city, metric, today, days):
    """% change of metric vs the closest stored day at least `days` ago; None if not enough history."""
    ref = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    old = con.execute(f"SELECT {metric} FROM area_history WHERE city=? AND date<=? AND {metric} IS NOT NULL "
                      "ORDER BY date DESC LIMIT 1", (city, ref)).fetchone()
    new = con.execute(f"SELECT {metric} FROM area_history WHERE city=? AND date=?", (city, today)).fetchone()
    if not old or not new or not old[0] or new[0] is None: return None
    return (new[0] - old[0]) / old[0] * 100

def first_date(con, city):
    r = con.execute("SELECT MIN(date) FROM area_history WHERE city=?", (city,)).fetchone()
    return r[0] if r else None

def sparkline(values, w=120, h=28):
    vals = [v for v in values if v is not None]
    if len(vals) < 2: return ""
    lo, hi = min(vals), max(vals); rng = (hi - lo) or 1
    n = len(vals)
    pts = " ".join(f"{i * (w - 4) / (n - 1) + 2:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}" for i, v in enumerate(vals))
    last = pts.split()[-1].split(",")
    color = "#1e8e3e" if vals[-1] <= vals[0] else "#c5221f"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><polyline fill="none" stroke="{color}" '
            f'stroke-width="1.6" points="{pts}"/><circle cx="{last[0]}" cy="{last[1]}" r="2.2" fill="{color}"/></svg>')

def sold_summary(recs, today, lot_min=None):
    """recs: HomeHarvest sold records (dicts). Medians for last 30 and 90 days."""
    rows = []
    for r in recs:
        p = r.get("sold_price"); d = str(r.get("last_sold_date") or "")[:10]
        try: p = float(p)
        except (TypeError, ValueError): continue
        if p != p or not d or d == "NaT": continue
        try: sq = float(r.get("sqft")); sq = sq if sq == sq and sq > 0 else None
        except (TypeError, ValueError): sq = None
        try: lot = float(r.get("lot_sqft")); lot = lot if lot == lot else None
        except (TypeError, ValueError): lot = None
        if lot_min and (lot is None or lot < lot_min): continue
        rows.append({"price": p, "date": d, "ppsf": p / sq if sq else None})
    out = {}
    for days in (30, 90):
        cut = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
        sel = [x for x in rows if x["date"] >= cut]
        out[days] = {"n": len(sel), "median": med([x["price"] for x in sel]), "ppsf": med([x["ppsf"] for x in sel])}
    return out
