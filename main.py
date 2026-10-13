#!/usr/bin/env python3
"""
Evidence-Based Diet Policy Rule Engine  (single file, Python 3.8+, NO installs)

Run in VS Code: open this file and press Run  (or:  python diet_policy_engine.py)

What it does
  * Pulls nutrient data from the USDA FoodData Central (FDC) API
  * Lets you build a meal plan from a menu
  * Checks the plan against codified diet policies (low-sodium, diabetic, DGA)
  * Never treats missing nutrient data as zero -> marks the check UNVERIFIED

API key
  You don't need one to start: the program falls back to USDA's shared DEMO_KEY
  (very low limits). Get a free key at https://fdc.nal.usda.gov/api-key-signup
  then put it in fdc_api_key.txt next to this file, or set the environment variable FDC_API_KEY.

DISCLAIMER: policy limits are EXAMPLE values for a school project. Real clinical
use needs the hospital's approved diet manual and a dietitian's sign-off.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# =====================================================================
# 0) SETTINGS / FILES
# =====================================================================
BASE_URL = "https://api.nal.usda.gov/fdc/v1"
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "usda_cache.json"
KEY_FILE = HERE / "fdc_api_key.txt"          # keep private, don't share/commit
PLAN_FILE = HERE / "my_meal_plan.json"
REPORT_FILE = HERE / "compliance_report.txt"

MEALS = ["breakfast", "lunch", "dinner", "snack"]
MAIN_MEALS = ["breakfast", "lunch", "dinner"]
SETTINGS = {"include_branded": False}

# key: (label, unit, USDA nutrient ids in priority order, legacy nutrient numbers)
NUTRIENTS = {
    "calories":      ("Energy",        "kcal", [1008, 2047, 2048], ["208", "957", "958"]),
    "sodium_mg":     ("Sodium",        "mg",   [1093],             ["307"]),
    "carbs_g":       ("Carbohydrate",  "g",    [1005],             ["205"]),
    "fiber_g":       ("Fiber",         "g",    [1079],             ["291"]),
    "added_sugar_g": ("Added sugars",  "g",    [1235],             ["539"]),
    "sat_fat_g":     ("Saturated fat", "g",    [1258],             ["606"]),
    "protein_g":     ("Protein",       "g",    [1003],             ["203"]),
}

# =====================================================================
# 1) POLICIES (rules as data - edit/add freely)
#   max / min        : whole-day total vs limit
#   meal_max/min     : each meal (rule["meals"], default main meals) vs limit
#   share_max        : nutrient calories as % of day's calories (needs kcal_per_g)
#   density_min      : nutrient per 1000 kcal
#   spread_max       : max-min of nutrient across main meals
#   meals_present    : at least `limit` main meals contain food
# =====================================================================
POLICIES = {
    "low_sodium": {
        "name": "Hospital 2 g Sodium Diet (cardiac / hypertension)",
        "population": "Adult inpatients on a 2,000 mg sodium diet",
        "sources": ["WHO. Guideline: Sodium intake for adults and children (2012)",
                    "American Heart Association sodium recommendations",
                    "Dietary Guidelines for Americans 2020-2025",
                    "Hospital '2 g Na' therapeutic diet convention (replace with your diet manual)"],
        "rules": [
            {"id": "NA-DAY", "type": "max", "nutrient": "sodium_mg", "limit": 2000, "severity": "error",
             "fix": "Swap salty/processed foods for fresh, unsalted ones."},
            {"id": "NA-MEAL", "type": "meal_max", "nutrient": "sodium_mg", "limit": 800, "severity": "warning",
             "fix": "Spread sodium more evenly across meals."},
            {"id": "SATFAT", "type": "share_max", "nutrient": "sat_fat_g", "kcal_per_g": 9, "limit": 10,
             "severity": "warning", "fix": "Prefer lean protein, low-fat dairy, unsaturated fats."},
        ],
    },
    "diabetic": {
        "name": "Hospital Consistent-Carbohydrate (Diabetic) Diet",
        "population": "Adult inpatients with diabetes",
        "sources": ["ADA Standards of Care: Diabetes Care in the Hospital (annual)",
                    "Evert AB et al. Nutrition Therapy for Adults With Diabetes or Prediabetes. Diabetes Care 2019;42(5):731-754",
                    "Dietary Guidelines for Americans 2020-2025"],
        "rules": [
            {"id": "CARB-MEAL-MAX", "type": "meal_max", "nutrient": "carbs_g", "limit": 60, "severity": "error",
             "fix": "Reduce starch/sugar portions at this meal."},
            {"id": "CARB-MEAL-MIN", "type": "meal_min", "nutrient": "carbs_g", "limit": 30, "severity": "warning",
             "fix": "Add a carbohydrate serving (whole grain, fruit) to avoid hypoglycemia risk."},
            {"id": "CARB-SNACK", "type": "meal_max", "nutrient": "carbs_g", "limit": 30, "meals": ["snack"],
             "severity": "warning", "fix": "Make the snack smaller or more protein/fiber-based."},
            {"id": "CARB-CONSISTENT", "type": "spread_max", "nutrient": "carbs_g", "limit": 15,
             "severity": "warning", "fix": "Even out carbohydrate grams across main meals."},
            {"id": "MEALS-3", "type": "meals_present", "limit": 3, "severity": "error",
             "fix": "Provide breakfast, lunch and dinner."},
            {"id": "ADDED-SUGAR", "type": "share_max", "nutrient": "added_sugar_g", "kcal_per_g": 4, "limit": 10,
             "severity": "error", "fix": "Remove sweetened drinks/desserts."},
            {"id": "SATFAT", "type": "share_max", "nutrient": "sat_fat_g", "kcal_per_g": 9, "limit": 10,
             "severity": "warning", "fix": "Prefer lean protein and unsaturated fats."},
            {"id": "FIBER", "type": "density_min", "nutrient": "fiber_g", "limit": 14, "severity": "warning",
             "fix": "Add whole grains, legumes, vegetables (14 g per 1000 kcal)."},
            {"id": "NA-DAY", "type": "max", "nutrient": "sodium_mg", "limit": 2300, "severity": "warning",
             "fix": "Cut processed foods and condiments."},
        ],
    },
    "dga_general": {
        "name": "National Dietary Guidelines - General Adult Baseline",
        "population": "Healthy adult general-diet menus",
        "sources": ["Dietary Guidelines for Americans 2020-2025 (USDA/HHS)",
                    "WHO. Guideline: Sugars intake for adults and children (2015)"],
        "rules": [
            {"id": "NA-DAY", "type": "max", "nutrient": "sodium_mg", "limit": 2300, "severity": "error",
             "fix": "Reduce processed foods, cured meats and condiments."},
            {"id": "ADDED-SUGAR", "type": "share_max", "nutrient": "added_sugar_g", "kcal_per_g": 4, "limit": 10,
             "severity": "error", "fix": "Remove sweetened beverages and desserts."},
            {"id": "SATFAT", "type": "share_max", "nutrient": "sat_fat_g", "kcal_per_g": 9, "limit": 10,
             "severity": "error", "fix": "Prefer lean protein, low-fat dairy, unsaturated oils."},
            {"id": "FIBER", "type": "density_min", "nutrient": "fiber_g", "limit": 14, "severity": "warning",
             "fix": "Add whole grains, legumes, vegetables, fruit."},
        ],
    },
}

# =====================================================================
# 2) OFFLINE SAMPLE FOODS (per 100 g, ILLUSTRATIVE approximations)
# =====================================================================
def _f(name, cal, na, carb, fib, add, sat, pro):
    return {"name": name, "fdc_id": None,
            "per100": dict(zip(NUTRIENTS, [cal, na, carb, fib, add, sat, pro]))}

SAMPLE_FOODS = {
    "oatmeal, cooked":          _f("Oatmeal, cooked (sample)",           71,   4, 12.0, 1.7,  0.0, 0.4, 2.5),
    "orange":                   _f("Orange (sample)",                    47,   0, 11.8, 2.4,  0.0, 0.0, 0.9),
    "pancakes with syrup":      _f("Pancakes with syrup (sample)",      227, 439, 28.0, 1.0,  8.0, 1.5, 6.0),
    "soda, regular":            _f("Soda, regular (sample)",             41,  11, 10.6, 0.0, 10.6, 0.0, 0.0),
    "deli ham":                 _f("Deli ham (sample)",                 107,1200,  1.5, 0.0,  0.5, 1.0,17.0),
    "canned chicken noodle soup": _f("Canned chicken noodle soup (sample)", 36, 340, 4.5, 0.3, 0.3, 0.4, 2.4),
    "grilled chicken breast":   _f("Grilled chicken breast, unsalted (sample)", 165, 74, 0.0, 0.0, 0.0, 1.0, 31.0),
    "brown rice, cooked":       _f("Brown rice, cooked (sample)",       123,   4, 25.6, 1.6,  0.0, 0.2, 2.7),
    "whole-wheat pasta, cooked": _f("Whole-wheat pasta, cooked (sample)",124,   3, 26.5, 3.9,  0.0, 0.2, 5.3),
    "broccoli, steamed":        _f("Broccoli, steamed (sample)",         35,  41,  7.2, 3.3,  0.0, 0.0, 2.4),
    "baked salmon":             _f("Baked salmon (sample)",             206,  61,  0.0, 0.0,  0.0, 3.1,22.0),
    "green salad, oil & vinegar": _f("Green salad, oil & vinegar (sample)", 55, 28, 3.3, 2.1, 0.0, 0.5, 1.2),
    "white toast":              _f("White toast (sample)",              270, 490, 50.0, 2.4,  3.0, 0.7, 9.0),
    "chocolate cake":           _f("Chocolate cake, frosted (sample)",  371, 369, 53.0, 2.3, 34.0, 4.0, 4.0),
    "almonds, unsalted":        _f("Almonds, unsalted (sample)",        579,   1, 21.6,12.5,  0.0, 3.8,21.2),
}

def sample_item(key, grams):
    food = SAMPLE_FOODS[key]
    return {"name": food["name"], "fdc_id": None, "per100": dict(food["per100"]), "grams": grams}

SAMPLE_PLANS = {
    "good": {
        "breakfast": [sample_item("oatmeal, cooked", 240), sample_item("orange", 130)],
        "lunch":     [sample_item("grilled chicken breast", 90), sample_item("brown rice, cooked", 150),
                      sample_item("broccoli, steamed", 150)],
        "dinner":    [sample_item("baked salmon", 100), sample_item("whole-wheat pasta, cooked", 150),
                      sample_item("green salad, oil & vinegar", 100), sample_item("broccoli, steamed", 100)],
        "snack":     [sample_item("almonds, unsalted", 15)],
    },
    "bad": {
        "breakfast": [sample_item("pancakes with syrup", 250), sample_item("soda, regular", 360)],
        "lunch":     [sample_item("deli ham", 100), sample_item("white toast", 60),
                      sample_item("canned chicken noodle soup", 250)],
        "dinner":    [sample_item("grilled chicken breast", 100), sample_item("brown rice, cooked", 200)],
        "snack":     [sample_item("chocolate cake", 100)],
    },
}

# =====================================================================
# 3) USDA FOODDATA CENTRAL API CLIENT
# =====================================================================
class APIError(Exception):
    pass

API_STATE = {"remaining": None}   # last X-RateLimit-Remaining header seen


def get_key():
    """Returns (key, source). Order: env var > saved file > DEMO_KEY."""
    env = os.environ.get("FDC_API_KEY", "").strip()
    if env:
        return env, "environment variable FDC_API_KEY"
    if KEY_FILE.exists():
        saved = KEY_FILE.read_text().strip()
        if saved:
            return saved, f"saved file {KEY_FILE.name}"
    return "DEMO_KEY", "none set - using shared DEMO_KEY (very low limits)"


def api_get(path, params):
    """GET request to FDC. Raises APIError with a friendly message."""
    key, _ = get_key()
    query = urllib.parse.urlencode({**params, "api_key": key})
    req = urllib.request.Request(f"{BASE_URL}/{path}?{query}",
                                 headers={"User-Agent": "diet-policy-engine/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            API_STATE["remaining"] = resp.headers.get("X-RateLimit-Remaining")
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise APIError("USDA rejected the API key (invalid or blocked). Check the key in fdc_api_key.txt.")
        if e.code == 404:
            raise APIError("Not found (check the FDC ID).")
        if e.code == 429:
            raise APIError("USDA rate limit reached. Wait a while, or register a free key. "
                           "Cached results still work.")
        raise APIError(f"USDA returned HTTP {e.code}.")
    except urllib.error.URLError as e:
        raise APIError(f"Could not reach USDA ({e.reason}). Check your internet connection.")
    except (TimeoutError, json.JSONDecodeError, OSError) as e:
        raise APIError(f"Problem talking to USDA: {e}")


def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    try:
        CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def extract_nutrients(entries):
    """Normalise the 3 shapes FDC uses (search / abridged / full) into our keys.
    Missing nutrients stay None (never silently 0)."""
    by_id, by_num = {}, {}
    for e in entries or []:
        nut = e.get("nutrient") or {}
        nid = e.get("nutrientId") or nut.get("id")
        num = e.get("nutrientNumber") or e.get("number") or nut.get("number")
        val = e.get("value", e.get("amount"))
        if val is None:
            continue
        if nid is not None:
            by_id[int(nid)] = float(val)
        if num is not None:
            by_num[str(num)] = float(val)
    out = {}
    for key, (_, _, ids, nums) in NUTRIENTS.items():
        v = next((by_id[i] for i in ids if i in by_id), None)
        if v is None:
            v = next((by_num[n] for n in nums if n in by_num), None)
        out[key] = v
    return out


def simplify_food(raw):
    return {"name": raw.get("description", "unknown food"),
            "fdc_id": raw.get("fdcId"),
            "data_type": raw.get("dataType", ""),
            "brand": raw.get("brandOwner", ""),
            "per100": extract_nutrients(raw.get("foodNutrients"))}


def usda_search(query, page_size=10):
    types = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
    if SETTINGS["include_branded"]:
        types.append("Branded")
    cache, ckey = load_cache(), f"search|{query.lower()}|{','.join(types)}"
    if ckey in cache:
        return cache[ckey], True
    data = api_get("foods/search", {"query": query, "pageSize": page_size, "dataType": ",".join(types)})
    results = [simplify_food(f) for f in data.get("foods", [])]
    cache[ckey] = results
    save_cache(cache)
    return results, False


def usda_get_food(fdc_id):
    cache, ckey = load_cache(), f"food|{fdc_id}"
    if ckey in cache:
        return cache[ckey], True
    raw = api_get(f"food/{fdc_id}", {"format": "abridged"})
    food = simplify_food(raw)
    cache[ckey] = food
    save_cache(cache)
    return food, False

# =====================================================================
# 4) VALIDATION ENGINE
# =====================================================================
EPS = 1e-9


def total(items, nutrient):
    """(sum of known values, True if any item lacks this nutrient)"""
    s, missing = 0.0, False
    for it in items:
        v = it["per100"].get(nutrient)
        if v is None:
            missing = True
        else:
            s += v * it["grams"] / 100.0
    return s, missing


def all_items(plan):
    return [it for m in MEALS for it in plan.get(m, [])]


def bound(kind, value, limit, missing):
    """PASS / FAIL / UNVERIFIED. Missing data can't be assumed to be zero."""
    if kind == "max":
        if value > limit + EPS:
            return "FAIL"
        return "UNVERIFIED" if missing else "PASS"
    if value >= limit - EPS:
        return "PASS"
    return "UNVERIFIED" if missing else "FAIL"


def evaluate(plan, policy):
    checks = []

    def add(rule, scope, status, text):
        checks.append({"rule": rule, "scope": scope, "status": status, "text": text})

    for r in policy["rules"]:
        t, n = r["type"], r.get("nutrient")
        label, unit = (NUTRIENTS[n][0], NUTRIENTS[n][1]) if n else ("", "")
        lim = r["limit"]

        if t in ("max", "min"):
            v, miss = total(all_items(plan), n)
            st = bound(t, v, lim, miss)
            cmp = "max" if t == "max" else "min"
            add(r, "whole day", st, f"{label} {v:.1f} {unit} ({cmp} {lim:g})")

        elif t in ("meal_max", "meal_min"):
            kind = "max" if t == "meal_max" else "min"
            for m in r.get("meals", MAIN_MEALS):
                if not plan.get(m):
                    continue
                v, miss = total(plan[m], n)
                add(r, m, bound(kind, v, lim, miss), f"{label} {v:.1f} {unit} ({kind} {lim:g})")

        elif t == "share_max":
            kcal, kmiss = total(all_items(plan), "calories")
            v, miss = total(all_items(plan), n)
            if kcal <= 0:
                add(r, "whole day", "UNVERIFIED", f"{label} share cannot be computed (no calorie data)")
                continue
            share = v * r["kcal_per_g"] / kcal * 100
            if share > lim + EPS and not kmiss:
                st = "FAIL"
            elif miss or kmiss:
                st = "UNVERIFIED"
            else:
                st = "PASS"
            add(r, "whole day", st, f"{label} = {share:.1f}% of calories (max {lim:g}%)")

        elif t == "density_min":
            kcal, kmiss = total(all_items(plan), "calories")
            v, miss = total(all_items(plan), n)
            if kcal <= 0:
                add(r, "whole day", "UNVERIFIED", f"{label} density cannot be computed (no calorie data)")
                continue
            dens = v / kcal * 1000
            if dens >= lim - EPS and not kmiss:
                st = "PASS"
            elif miss or kmiss:
                st = "UNVERIFIED"
            else:
                st = "FAIL"
            add(r, "whole day", st, f"{label} {dens:.1f} {unit}/1000 kcal (min {lim:g})")

        elif t == "spread_max":
            vals, miss = [], False
            for m in MAIN_MEALS:
                if plan.get(m):
                    v, mm = total(plan[m], n)
                    vals.append(v)
                    miss = miss or mm
            if len(vals) < 2:
                continue
            spread = max(vals) - min(vals)
            st = "UNVERIFIED" if miss else ("FAIL" if spread > lim + EPS else "PASS")
            add(r, "main meals", st, f"{label} spread {spread:.1f} {unit} (max {lim:g})")

        elif t == "meals_present":
            count = sum(1 for m in MAIN_MEALS if plan.get(m))
            add(r, "whole day", "PASS" if count >= lim else "FAIL",
                f"{count} of 3 main meals provided (need {lim:g})")
    return checks


def overall_status(checks):
    fails = [c for c in checks if c["status"] == "FAIL"]
    unv = [c for c in checks if c["status"] == "UNVERIFIED"]
    if any(c["rule"]["severity"] == "error" for c in fails):
        return "NON-COMPLIANT"
    if any(c["rule"]["severity"] == "error" for c in unv):
        return "INDETERMINATE (missing data on a required rule)"
    if fails or unv:
        return "COMPLIANT (with warnings)"
    return "COMPLIANT"


def render_report(plan, pkey):
    pol = POLICIES[pkey]
    checks = evaluate(plan, pol)
    passed = [c for c in checks if c["status"] == "PASS"]
    lines = [f"\n=== {pol['name']} ===",
             f"Population: {pol['population']}",
             "Evidence:  " + "\n           ".join(pol["sources"]),
             f"RESULT: {overall_status(checks)}   "
             f"({len(passed)}/{len(checks)} checks passed)"]
    for c in checks:
        if c["status"] == "PASS":
            continue
        tag = "FAIL" if c["status"] == "FAIL" else "UNVERIFIED"
        lines.append(f"  [{tag}][{c['rule']['severity'].upper()}] {c['rule']['id']} @ {c['scope']}: {c['text']}")
        if c["status"] == "UNVERIFIED":
            lines.append("        Some foods lack this nutrient in USDA data - enter it manually or swap the food.")
        lines.append(f"        Fix: {c['rule']['fix']}")
    bad_ids = {c["rule"]["id"] for c in checks if c["status"] != "PASS"}
    ok_ids = sorted({c["rule"]["id"] for c in passed} - bad_ids)
    if ok_ids:
        lines.append("  Fully passed: " + ", ".join(ok_ids))
    return "\n".join(lines), overall_status(checks), len(passed), len(checks)

# =====================================================================
# 5) INPUT HELPERS
# =====================================================================
def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye!")
        raise SystemExit(0)


def ask_choice(prompt, lo, hi):
    """Integer in [lo, hi]; blank -> None."""
    while True:
        s = ask(prompt)
        if s == "":
            return None
        if s.isdigit() and lo <= int(s) <= hi:
            return int(s)
        print(f"  Please enter a number from {lo} to {hi} (or press Enter to go back).")


def ask_float(prompt, default=None):
    while True:
        s = ask(prompt)
        if s == "" and default is not None:
            return default
        try:
            v = float(s)
            if v > 0:
                return v
        except ValueError:
            pass
        print("  Please enter a positive number.")


def fmt(v):
    return "n/a" if v is None else f"{round(v, 1):g}"


def pause():
    ask("\n(press Enter to continue) ")

# =====================================================================
# 6) MENU ACTIONS
# =====================================================================
PLAN = {m: [] for m in MEALS}


def show_foods_table(foods):
    print(f"\n  {'#':>2}  {'Food (values per 100 g)':<50} {'kcal':>5} {'Na mg':>6} {'carb':>5} {'fiber':>5} {'addSug':>6}")
    for i, f in enumerate(foods, 1):
        p = f["per100"]
        name = f["name"][:46]
        tag = f" ({f['data_type'][:4]})" if f.get("data_type") else ""
        print(f"  {i:>2}. {(name + tag)[:50]:<50} {fmt(p['calories']):>5} {fmt(p['sodium_mg']):>6} "
              f"{fmt(p['carbs_g']):>5} {fmt(p['fiber_g']):>5} {fmt(p['added_sugar_g']):>6}")
    print("  (n/a = USDA has no value for this nutrient; the engine will treat it as UNVERIFIED, not zero)")


def search_usda_interactive():
    """Search and let the user pick. Returns a food dict or None."""
    q = ask("Search term (e.g. 'chicken breast', Enter to cancel): ")
    if not q:
        return None
    try:
        foods, cached = usda_search(q)
    except APIError as e:
        print(f"\n  ! {e}")
        print("  Tip: option 6 loads an offline sample plan so you can still demo the validation.")
        return None
    if not foods:
        print("  No results. Try a simpler word.")
        return None
    print(f"\n  {len(foods)} result(s){' (from local cache)' if cached else ''}:")
    show_foods_table(foods)
    c = ask_choice("Pick a food number (Enter to cancel): ", 1, len(foods))
    return foods[c - 1] if c else None




def fill_missing(food):
    missing = [k for k, v in food["per100"].items() if v is None]
    if not missing:
        return
    names = ", ".join(NUTRIENTS[k][0] for k in missing)
    print(f"\n  USDA has no value for: {names}")
    print("  [Z] treat all as 0 (only if you're sure, e.g. plain vegetables have no added sugar)")
    print("  [E] enter values one by one     [Enter] leave unknown (checks become UNVERIFIED)")
    c = ask("  Choice: ").lower()
    if c == "z":
        for k in missing:
            food["per100"][k] = 0.0
    elif c == "e":
        for k in missing:
            s = ask(f"  {NUTRIENTS[k][0]} per 100 g ({NUTRIENTS[k][1]}), Enter = unknown: ")
            try:
                food["per100"][k] = float(s) if s else None
            except ValueError:
                pass


def add_food_to_plan(food):
    fill_missing(food)
    print("\n  Which meal?")
    for i, m in enumerate(MEALS, 1):
        print(f"   {i}. {m}")
    mi = ask_choice("  Meal number (Enter to cancel): ", 1, len(MEALS))
    if not mi:
        return
    grams = ask_float("  Amount in grams (Enter = 100): ", default=100.0)
    PLAN[MEALS[mi - 1]].append({"name": food["name"], "fdc_id": food.get("fdc_id"),
                                "per100": food["per100"], "grams": grams})
    print(f"  Added {grams:g} g of '{food['name']}' to {MEALS[mi - 1]}.")


def menu_search():
    """API retrieval demo: search USDA, pick a result, add it to the plan."""
    food = search_usda_interactive()
    if food:
        add_food_to_plan(food)


def menu_clear():
    for m in MEALS:
        PLAN[m].clear()
    print("\n  Meal plan cleared.")


def menu_test_api():
    key, source = get_key()
    masked = "DEMO_KEY" if key == "DEMO_KEY" else key[:4] + "*" * 8
    print(f"\n  Key in use: {masked}  ({source})")
    try:
        data = api_get("foods/search", {"query": "apple", "pageSize": 1})
        print(f"  OK - connected to USDA FoodData Central ({data.get('totalHits', '?')} hits for 'apple').")
        if API_STATE["remaining"]:
            print(f"  Requests remaining this hour: {API_STATE['remaining']}")
    except APIError as e:
        print(f"  ! {e}")




def plan_count():
    return len(all_items(PLAN))


def menu_view_plan():
    if not plan_count():
        print("\n  Your meal plan is empty. Use option 1 (search USDA) or 6 (load sample plan).")
        return
    print("\n--- YOUR MEAL PLAN ---")
    for m in MEALS:
        if PLAN[m]:
            print(f" {m.upper()}")
            for it in PLAN[m]:
                kcal = it["per100"].get("calories")
                kcal_s = "n/a" if kcal is None else f"{kcal * it['grams'] / 100:.0f}"
                print(f"   - {it['name'][:52]}: {it['grams']:g} g  (~{kcal_s} kcal)")
    print("\n DAILY TOTALS")
    items = all_items(PLAN)
    for k, (label, unit, _, _) in NUTRIENTS.items():
        v, miss = total(items, k)
        print(f"   {label:<14} {v:>8.1f} {unit}{'   (incomplete: some foods lack data)' if miss else ''}")



def choose_policy():
    keys = list(POLICIES)
    print("\n  Policies:")
    for i, k in enumerate(keys, 1):
        print(f"   {i}. {POLICIES[k]['name']}")
    c = ask_choice("  Pick a policy (Enter to cancel): ", 1, len(keys))
    return keys[c - 1] if c else None


def maybe_save_report(text):
    if ask("\nSave this report to a text file? (y/N): ").lower() == "y":
        try:
            REPORT_FILE.write_text(text + "\n", encoding="utf-8")
            print(f"  Saved to {REPORT_FILE}")
        except OSError as e:
            print(f"  Could not save: {e}")


def menu_validate_one():
    if not plan_count():
        print("\n  Your meal plan is empty. Search foods first (option 1) or load a sample (option 6).")
        return
    k = choose_policy()
    if k:
        text = render_report(PLAN, k)[0]
        print(text)
        maybe_save_report(text)


def menu_validate_all():
    if not plan_count():
        print("\n  Your meal plan is empty. Search foods first (option 1) or load a sample (option 6).")
        return
    print(f"\n  {'Policy':<52} {'Result':<42} Checks passed")
    texts = []
    for k in POLICIES:
        text, status, p, n = render_report(PLAN, k)
        texts.append(text)
        print(f"  {POLICIES[k]['name'][:50]:<52} {status[:40]:<42} {p}/{n}")
    if ask("\nShow full details for every policy? (y/N): ").lower() == "y":
        full = "\n".join(texts)
        print(full)
        maybe_save_report(full)


def menu_policies():
    k = choose_policy()
    if not k:
        return
    pol = POLICIES[k]
    print(f"\n=== {pol['name']} ===\nPopulation: {pol['population']}\nRules:")
    for r in pol["rules"]:
        what = {"max": "daily max", "min": "daily min", "meal_max": "per-meal max", "meal_min": "per-meal min",
                "share_max": "max % of calories", "density_min": "min per 1000 kcal",
                "spread_max": "max spread across meals", "meals_present": "main meals required"}[r["type"]]
        print(f"  [{r['severity'].upper():<7}] {r['id']:<16} {r.get('nutrient', '') or '':<14} {what}: {r['limit']:g}")
    print("Evidence sources:")
    for s in pol["sources"]:
        print(f"  - {s}")
    print("Only ERROR rules make a plan non-compliant; WARNING rules are advisory.")


def menu_samples():
    print("\n  1. Load a COMPLIANT sample plan\n  2. Load a NON-COMPLIANT sample plan")
    c = ask_choice("  Choice (Enter to cancel): ", 1, 2)
    if c:
        src = SAMPLE_PLANS["good" if c == 1 else "bad"]
        for m in MEALS:
            PLAN[m] = [dict(it, per100=dict(it["per100"])) for it in src[m]]
        print("  Sample plan loaded (replaces current plan). Try options 2, 3 and 4.")




# =====================================================================
# 7) MAIN MENU
# =====================================================================
MENU = [
    ("Search USDA FoodData Central & add food to plan   [API demo]", menu_search),
    ("View my meal plan & daily totals", menu_view_plan),
    ("Validate plan against ONE policy", menu_validate_one),
    ("Validate plan against ALL policies (comparison)", menu_validate_all),
    ("View policy rules & evidence sources", menu_policies),
    ("Load a sample meal plan (offline demo)", menu_samples),
    ("Test USDA API connection", menu_test_api),
    ("Clear my meal plan", menu_clear),
]


def main():
    while True:
        key, _ = get_key()
        print("\n" + "=" * 62)
        print("   DIET POLICY COMPLIANCE ENGINE")
        print(f"   USDA key: {'DEMO_KEY (limited)' if key == 'DEMO_KEY' else 'personal key'}   |   "
              f"Plan: {plan_count()} item(s)")
        print("=" * 62)
        for i, (label, _) in enumerate(MENU, 1):
            print(f" {i:>2}. {label}")
        print("  0. Exit")
        choice = ask("\nChoose an option: ")
        if choice == "0":
            print("Goodbye! Remember: results are decision support, not clinical advice.")
            return
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            MENU[int(choice) - 1][1]()
            pause()
        else:
            print("  Invalid choice - enter a number from the menu.")


if __name__ == "__main__":
    main()
    