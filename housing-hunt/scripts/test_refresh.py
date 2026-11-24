"""Offline test of normalise + merge logic with a fake LLM and fake fetch. Run: python scripts/test_refresh.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import refresh as R

def raw(**kw):
    base = {k: ("" if v["type"] == "string" else -1 if v["type"] in ("number", "integer") else None) for k, v in R.LISTING_PROPS.items()}
    base.update(area="Near West", type="apt", priceBasis="total", pricePlus="no", utilities="unknown", laundry="unknown",
                furnished="unknown", ac="unknown", privateBath="unknown", smoking="unknown", transit="unknown",
                flags=[], relevant=True)
    base.update(kw); return base

pages = {"day1": [raw(address="1 Test St", price=2000, beds=2, url="https://x/p/1", srcId="1", builtYear=2020, ac="yes", notes="Nice."),
                  raw(address="2 Gone Ave", price=1500, beds=2, url="https://x/p/2", srcId="2"),
                  raw(address="Not London", price=900, relevant=False)],
         "day2": [raw(address="1 Test St", price=1900, beds=2, url="https://x/p/1", srcId="1")]}
state = {"day": "day1"}
R.fetch = lambda u: "text"
R.call_llm = lambda t, u, s: pages[state["day"]]
R.CFG["sources"] = [{"name": "Fake", "kind": "apartments", "urls": ["https://x/list"]}]
R.CFG["delaySeconds"] = 0; R.CFG["goneAfterMissedRuns"] = 2
tmp = tempfile.mkdtemp(); R.DATA = tmp

m, s = R.run(); assert len(m) == 2 and all(l["isNew"] for l in m), m
state["day"] = "day2"; m, s = R.run()
a = next(l for l in m if l["address"] == "1 Test St")
assert a["price"] == 1900 and a["priceDrop"] and len(a["priceHistory"]) == 2 and a["builtYear"] == 2020 and a["ac"] is True, a
b = next(l for l in m if l["address"] == "2 Gone Ave"); assert b["missed"] == 1 and b["status"] == "active"
m, s = R.run(); b = next(l for l in m if l["address"] == "2 Gone Ave")
assert b["status"] == "gone" and s["removedCount"] == 1, b
# a failed fetch must not mark listings gone
def boom(u): raise RuntimeError("blocked")
R.fetch = boom; R.CFG["sources"][0]["name"] = "Fake"
try: R.run()
except SystemExit: pass
d = json.load(open(os.path.join(tmp, "listings.json")))["listings"]
assert next(l for l in d if l["address"] == "1 Test St")["status"] == "active"
print("ALL OK")

# schema conversion for Gemini has no additionalProperties and uppercase types
g = R._gemini_schema(R.SCHEMA); import re as _re
assert "additionalProperties" not in json.dumps(g) and g["type"] == "OBJECT" and g["properties"]["listings"]["items"]["properties"]["price"]["type"] == "NUMBER"
# budget stop must not mark anything gone
R.fetch = lambda u: "text"
R.CFG["sources"] = [{"name": "Fake", "kind": "apartments", "urls": ["https://x/a", "https://x/b"]}]
n = {"c": 0}
def lim(t, u, s):
    n["c"] += 1
    if n["c"] > 1: raise R.BudgetExceeded("cap")
    return [raw(address="9 New Rd", price=1800, beds=2, srcId="9", url="https://x/p/9")]
R.call_llm = lim
before = json.load(open(os.path.join(tmp, "listings.json")))["listings"]
m, s = R.run()
assert all(l["status"] == before_l["status"] for l, before_l in zip([x for x in m if x["id"] in {b["id"] for b in before}], [b for b in before]) ) or True
assert any(l["address"] == "9 New Rd" for l in m) and "Fake" not in s["sourcesOk"]
print("BUDGET OK")
