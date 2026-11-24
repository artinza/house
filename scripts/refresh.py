#!/usr/bin/env python3
"""Daily housing refresh.

1. Download each source page with plain HTTP (GitHub runners have open internet).
2. Turn the HTML into compact text and ask Claude to extract listings as strict JSON.
3. Merge with yesterday's data (first seen, price history, gone listings) and write public/data/*.json.

Run locally:  GEMINI_API_KEY=... python scripts/refresh.py [--only "Zumper"] [--dry]
"""
import argparse, hashlib, json, os, re, sys, time, datetime as dt
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
DATA = os.path.join(ROOT, "public", "data")
TODAY = dt.date.today().isoformat()

AREAS = ["Old North", "Near South", "Near West", "Masonville", "Downtown", "Whitehills",
         "S. W. London", "Kipps Lane", "West London", "North London", "East London", "Other"]
TYPES = ["apt", "apt-share", "bachelor", "house-share", "house", "room", "other", "unknown"]
YN = ["yes", "no", "unknown"]

# Every field is required and has a sentinel for unknown (-1, "" or "unknown").
# That keeps the schema simple enough for strict structured output.
LISTING_PROPS = {
    "srcId": {"type": "string"}, "url": {"type": "string"}, "address": {"type": "string"},
    "area": {"type": "string", "enum": AREAS}, "type": {"type": "string", "enum": TYPES},
    "price": {"type": "number"}, "priceBasis": {"type": "string", "enum": ["perBed", "total", "unknown"]},
    "pricePlus": {"type": "string", "enum": YN},
    "beds": {"type": "integer"}, "baths": {"type": "number"}, "distKm": {"type": "number"},
    "utilities": {"type": "string", "enum": ["included", "partial", "extra", "unknown"]},
    "utilNote": {"type": "string"}, "availFrom": {"type": "string"}, "leaseMonths": {"type": "integer"},
    "builtYear": {"type": "integer"}, "laundry": {"type": "string", "enum": ["yes", "shared", "no", "unknown"]},
    "furnished": {"type": "string", "enum": ["full", "common", "no", "unknown"]},
    "parking": {"type": "integer"}, "ac": {"type": "string", "enum": YN},
    "privateBath": {"type": "string", "enum": YN}, "smoking": {"type": "string", "enum": YN},
    "transit": {"type": "string", "enum": YN}, "tenantType": {"type": "string"},
    "flags": {"type": "array", "items": {"type": "string"}}, "deal": {"type": "string"},
    "notes": {"type": "string"}, "relevant": {"type": "boolean"},
}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["listings"], "properties": {
    "listings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": list(LISTING_PROPS), "properties": LISTING_PROPS}}}}

SYSTEM = f"""You extract rental listings for a student housing search at Western University, London, Ontario.
Today is {TODAY}. The renters are two first-year engineering students who want to move in around May 2027.
Rules:
- Return only listings that appear in the supplied page text. Never invent or guess facts.
- Use -1 for unknown numbers, "" for unknown text and "unknown" for unknown enums.
- price is a monthly rent in CAD. priceBasis is perBed when the price is per bedroom or per room, total when it is for the whole unit.
- pricePlus is yes when the text says the price is a starting price or "+" or plus utilities.
- utilities: included (all in), partial (some in, put which in utilNote), extra (tenant pays).
- distKm only when the page states a distance to Western. Do not estimate it.
- availFrom as YYYY-MM-DD when a date is given. builtYear only when stated.
- flags: short lowercase strings. Use "new build" for new construction, "renovated" for recent renovation, and warnings such as "price needs confirming", "dates conflict", "bedroom count unclear" when the page contradicts itself.
- deal: a one line promotion such as "1 month free" or "". notes: one plain sentence, max 25 words.
- area: pick the closest Western student neighbourhood from the enum, else Other.
- relevant: false for anything that is not a rental in London Ontario, is a roommate wanted ad for one bed in someone else's home, a short term or vacation rental, or clearly not a long lease.
- srcId is the site's own listing id if visible, else "". url is the listing's own page if visible, else the page url."""

# ---------------------------------------------------------------- fetching
def html_to_text(html, base):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        t.decompose()
    for a in soup.find_all("a", href=True):
        h = urljoin(base, a["href"])
        if any(k in h for k in ("/Listings/Details", "/apartments-for-rent/", "/v-", "/rental", "/listing", "/p/", "/building")):
            a.append(f" <{h}>")
    for img in soup.find_all("img"):
        img.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[: CFG["maxCharsPerPage"]]

def fetch(url, tries=3):
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept-Language": "en-CA,en;q=0.9"}
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=hdr, timeout=30)
            if r.status_code == 200 and r.text:
                return html_to_text(r.text, url)
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except Exception as e:  # network trouble
            last = repr(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")

# ---------------------------------------------------------------- LLM
class BudgetExceeded(Exception):
    pass

_client = None
_calls = 0

def _gemini_schema(node):
    """Convert our JSON schema to the subset Gemini accepts."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "additionalProperties":
                continue
            out[k] = v.upper() if k == "type" and isinstance(v, str) else _gemini_schema(v)
        return out
    if isinstance(node, list):
        return [_gemini_schema(x) for x in node]
    return node

_model = None
def pick_model():
    """Use config model if it exists, else the newest plain Gemini flash model this key can call."""
    global _model
    if _model:
        return _model
    import requests
    key = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    names = []
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
    while url:
        j = requests.get(url, headers=key, timeout=30).json()
        names += [m["name"].split("/")[-1] for m in j.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        url = ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&pageToken=" + j["nextPageToken"]) if j.get("nextPageToken") else None
    want = CFG.get("model")
    if want in names:
        _model = want
    else:
        def ver(n):
            m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash", n)
            return float(m.group(1)) if m else -1
        flash = sorted((n for n in names if ver(n) > 0), key=ver, reverse=True)
        lite = sorted((n for n in names if re.fullmatch(r"gemini-[\d.]+-flash-lite", n)), reverse=True)
        pool = flash or lite
        if not pool:
            raise RuntimeError("No usable Gemini model for this key. Models seen: " + ", ".join(names[:15]))
        _model = pool[0]
    print("Using Gemini model:", _model)
    return _model

def _post_gemini(prompt):
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{pick_model()}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": _gemini_schema(SCHEMA),
                                 "maxOutputTokens": 30000, "temperature": 0}}
    for attempt in range(4):
        r = requests.post(url, json=body, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=180)
        if r.status_code == 429:
            if "PerDay" in r.text or "per day" in r.text.lower():
                raise BudgetExceeded("daily free quota used up")
            time.sleep(20 * (attempt + 1)); continue
        if r.status_code >= 500:
            time.sleep(10 * (attempt + 1)); continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
        j = r.json()
        cand = (j.get("candidates") or [{}])[0]
        if cand.get("finishReason") == "MAX_TOKENS":
            raise OverflowError("truncated")
        return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    raise RuntimeError("Gemini kept failing")

def call_llm(text, page_url, source):
    """Returns a list of raw listing dicts. Replaced by a fake in tests."""
    global _client, _calls
    if _calls >= CFG.get("maxRequestsPerRun", 60):
        raise BudgetExceeded("per-run request cap reached")
    _calls += 1
    user = f"Source: {source['name']} ({source.get('note','')})\nPage: {page_url}\n\n--- PAGE TEXT ---\n{text}"
    if CFG.get("provider", "gemini") == "gemini":
        out = _post_gemini(user)
        time.sleep(CFG.get("llmDelaySeconds", 7))  # stay under the free per-minute limit
    else:
        import anthropic
        if _client is None:
            _client = anthropic.Anthropic()
        kw = dict(model=CFG["model"], max_tokens=16000, system=SYSTEM, messages=[{"role": "user", "content": user}])
        r = _client.messages.create(**kw, output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
        if r.stop_reason == "max_tokens":
            raise OverflowError("truncated")
        out = "".join(b.text for b in r.content if b.type == "text")
    out = re.sub(r"^```(?:json)?|```$", "", out.strip()).strip()
    return json.loads(out).get("listings", [])

def extract(text, page_url, source, depth=0):
    try:
        return call_llm(text, page_url, source)
    except OverflowError:
        if depth >= 2:
            return []
        mid = text.rfind("\n", 0, len(text) // 2)
        return extract(text[:mid], page_url, source, depth + 1) + extract(text[mid:], page_url, source, depth + 1)

# ---------------------------------------------------------------- normalise + merge
def unk(v):
    return v is None or v == "" or v == -1 or v == "unknown"

def yn(v):
    return True if v == "yes" else False if v == "no" else None

def normalise(raw, source):
    if not raw.get("relevant", True) or unk(raw.get("price")) or not raw.get("address"):
        return None
    src_id = str(raw.get("srcId") or "")
    url = raw.get("url") or ""
    if source["kind"] == "board":
        m = re.search(r"/Details/(\d+)", url)
        src_id = m.group(1) if m else src_id
    slug = re.sub(r"[^a-z0-9]+", "", source["name"].lower())[:8]
    if src_id:
        lid = f"{slug}_{src_id}"
    else:
        key = f"{raw['address'].lower()}|{raw.get('beds')}|{raw.get('price')}"
        lid = f"{slug}_{hashlib.md5(key.encode()).hexdigest()[:8]}"
    lid = re.sub(r"[^A-Za-z0-9_]", "", lid)
    d = {"id": lid, "address": raw["address"].strip(), "source": source["name"], "srcId": src_id, "url": url,
         "area": raw.get("area") or "Other", "type": raw.get("type") if raw.get("type") != "unknown" else None,
         "price": float(raw["price"]), "basis": raw.get("priceBasis", "unknown"),
         "pricePlus": raw.get("pricePlus") == "yes", "utilities": None if unk(raw.get("utilities")) else raw["utilities"],
         "flags": [f.strip().lower() for f in raw.get("flags", []) if f.strip()]}
    if d["price"].is_integer():
        d["price"] = int(d["price"])
    for k in ("beds", "leaseMonths", "builtYear", "parking", "baths", "distKm"):
        if not unk(raw.get(k)):
            d[k] = raw[k]
    for k in ("utilNote", "availFrom", "tenantType", "deal", "notes"):
        if raw.get(k):
            d[k] = raw[k]
    for k in ("laundry", "furnished"):
        if not unk(raw.get(k)):
            d[k] = raw[k]
    for k in ("ac", "privateBath", "smoking", "transit"):
        b = yn(raw.get(k))
        if b is not None:
            d[k] = b
    if d.get("distKm") is not None and d["distKm"] > 40:
        d.pop("distKm")
    # a row with only a summary has little beyond price and beds
    d["detail"] = sum(k in d for k in ("baths", "laundry", "furnished", "ac", "notes", "builtYear")) >= 2
    return d

def merge(old_list, fresh, ok_sources):
    """fresh: list of normalised dicts from this run. ok_sources: sources fetched successfully."""
    old = {l["id"]: l for l in old_list}
    seen = set()
    out = []
    removed = 0
    for n in fresh:
        seen.add(n["id"])
        o = old.get(n["id"])
        if not o:
            n.update(firstSeen=TODAY, lastSeen=TODAY, isNew=True, status="active", missed=0,
                     priceHistory=[{"d": TODAY, "p": n["price"]}], priceDrop=False)
            out.append(n)
            continue
        m = dict(o)
        for k, v in n.items():  # fresh known values win, unknowns never erase old data
            if k in ("detail",):
                continue
            m[k] = v
        m["detail"] = bool(o.get("detail") or n.get("detail"))
        hist = list(o.get("priceHistory") or [])
        if not hist or hist[-1]["p"] != n["price"]:
            hist.append({"d": TODAY, "p": n["price"]})
        m["priceHistory"] = hist
        m["priceDrop"] = len(hist) > 1 and hist[-1]["p"] < hist[-2]["p"]
        m["lastSeen"] = TODAY
        m["missed"] = 0
        m["status"] = "active"
        m["isNew"] = (dt.date.fromisoformat(TODAY) - dt.date.fromisoformat(o.get("firstSeen", TODAY))).days < 2
        out.append(m)
    for lid, o in old.items():
        if lid in seen:
            continue
        o = dict(o)
        o["isNew"] = False
        if o.get("source") in ok_sources:
            o["missed"] = o.get("missed", 0) + 1
            if o["missed"] >= CFG["goneAfterMissedRuns"] and o.get("status") != "gone":
                o["status"] = "gone"
                removed += 1
        out.append(o)
    return out, removed

# ---------------------------------------------------------------- run
def board_pages(src):
    for n in range(1, src.get("maxPages", 25) + 1):
        yield src["paged"].format(n=n)

def run(only=None, dry=False):
    path = os.path.join(DATA, "listings.json")
    old = json.load(open(path))["listings"] if os.path.exists(path) else []
    fresh, ok, errors = [], set(), []
    detail_budget = CFG["maxDetailFetchesPerRun"]
    out_of_budget = False
    for src in CFG["sources"]:
        if only and src["name"] != only:
            continue
        urls = list(board_pages(src)) if src["kind"] == "board" else src["urls"]
        got, failed = [], False
        for u in urls:
            try:
                text = fetch(u)
                rows = [normalise(r, src) for r in extract(text, u, src)]
                rows = [r for r in rows if r]
                print(f"{src['name']}: {u} -> {len(rows)} listings")
                got += rows
                if src["kind"] == "board" and not rows:
                    break  # ran past the last page
            except BudgetExceeded as e:
                errors.append(f"stopped early: {e}"); failed = True; out_of_budget = True
                print(f"STOP: {e}", file=sys.stderr)
                break
            except Exception as e:
                errors.append(str(e)[:200]); failed = True
                print(f"{src['name']}: FAILED {u}: {e}", file=sys.stderr)
                if src["kind"] == "board":
                    break
            time.sleep(CFG["delaySeconds"])
        if not failed and got:
            ok.add(src["name"])
        fresh += got[: CFG["maxListingsPerRun"]]
        if out_of_budget:
            break
    # fill in details for board listings we know little about
    known = {l["id"]: l for l in old}
    for n in fresh:
        if detail_budget <= 0 or out_of_budget:
            break
        if n["source"] != "Western Off-Campus Housing" or n["detail"] or known.get(n["id"], {}).get("detail"):
            continue
        src = next(s for s in CFG["sources"] if s["kind"] == "board")
        try:
            u = src["detail"].format(srcId=n["srcId"])
            rows = [normalise(r, src) for r in extract(fetch(u), u, src)]
            rows = [r for r in rows if r]
            if rows:
                d = rows[0]
                for k, v in d.items():
                    if k not in ("id", "address", "srcId") or not n.get(k):
                        n[k] = v
                n["id"] = n["id"]  # id stays stable
                n["detail"] = True
            detail_budget -= 1
        except BudgetExceeded:
            break
        except Exception as e:
            errors.append(str(e)[:200]); detail_budget -= 1
        time.sleep(CFG["delaySeconds"])
    merged, removed = merge(old, fresh, ok)
    merged.sort(key=lambda l: (l.get("status") == "gone", l["id"]))
    status = {"lastRun": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "removedCount": removed,
              "sourcesOk": sorted(ok), "errors": errors[:10], "total": len(merged)}
    print(json.dumps(status, indent=1))
    if dry:
        return merged, status
    if not fresh:
        print("No listings extracted at all, leaving data untouched.", file=sys.stderr)
        status["note"] = "Last refresh found nothing, check the Action log"
        json.dump(status, open(os.path.join(DATA, "status.json"), "w"), indent=1)
        sys.exit(1)
    os.makedirs(DATA, exist_ok=True)
    json.dump({"listings": merged}, open(path, "w"), indent=1, ensure_ascii=False)
    json.dump(status, open(os.path.join(DATA, "status.json"), "w"), indent=1)
    return merged, status

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    need = "GEMINI_API_KEY" if CFG.get("provider", "gemini") == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(need) and not a.dry:
        sys.exit("Set " + need)
    run(a.only, a.dry)
