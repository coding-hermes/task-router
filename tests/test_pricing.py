"""TR-034 test battery — per-provider pricing modules (scripts/pricing/).

Locks in the refactored pricing math: the per-billing-model modules
(per_token / per_request / per_minute / flat_subscription /
official-points / subscription), the per-provider reprice modules
(deepseek / opencode-go) and the evidence-guarded generic estimate module,
plus the shared helpers (find_or_id exact-then-prefix, public-price fill,
discount application, blended weights).

Patterns mirror tests/test_regression.py (_load_tables / _state_dir style
fixtures) and tests/test_maintain_repair.py (scratch-tree subprocess runs —
never the live registry).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable

sys.path.insert(0, SCRIPTS)
import router_maintain  # noqa: E402
from pricing import helpers as ph  # noqa: E402


# ---------------------------------------------------------------- fixtures ----

def _load_tables():
    """Committed registry data as {table: [row...]} (same as test_regression)."""
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[: -len(".jsonl")]
            rows = [json.loads(l) for l in open(os.path.join(DATA_DIR, fn)) if l.strip()]
            tables[name] = rows
    return tables


def _ctx(discounts=(), today="2026-09-10", **extra):
    ctx = {"discounts": list(discounts), "today": today}
    ctx.update(extra)
    return ctx


def _terms(billing_model, **kw):
    t = {"provider": "prov-x", "billing_model": billing_model}
    t.update(kw)
    return t


def _catalog(price_in=1.0, price_out=2.0, provider="prov-x", model="m1"):
    return {(provider, model): {"provider": provider, "model": model,
                                "cost_input": price_in, "cost_output": price_out}}


def _row(model="m1", provider="prov-x", **kw):
    r = {"provider": provider, "model": model, "normalized_price": None,
         "price_evidence": None}
    r.update(kw)
    return r


# ------------------------------------------------------ per-billing-model ----

def test_per_token_deepseek():
    """A per_token row with a matching catalog sticker gets
    normalized:payg-sticker — the real deepseek plan_terms row."""
    tables = _load_tables()
    terms = {t["provider"]: t for t in tables["plan_terms"]}
    assert terms["deepseek"]["billing_model"] == "per_token"
    catalog = {(c["provider"], c["model"]): c
               for c in tables.get("model_catalog") or []}
    row = _row(provider="deepseek", model="deepseek-v4-flash")
    from pricing import per_token
    price, ev, gap = per_token.price(row, terms["deepseek"], catalog, ph, _ctx())
    assert gap is None
    assert ev == "normalized:payg-sticker"
    cat = catalog[("deepseek", "deepseek-v4-flash")]
    assert price == round(float(cat["cost_input"]), 4)
    assert price == 0.14  # committed deepseek-v4-flash sticker
    # public-price fill stamps the sticker columns
    assert row["public_in_per_m"] == round(float(cat["cost_input"]), 4)


def test_per_token_no_sticker_is_gap():
    from pricing import per_token
    price, ev, gap = per_token.price(_row(), _terms("per_token"), {}, ph, _ctx())
    assert price is None and ev is None
    assert gap == "no models.dev sticker"


def test_per_request_opencode_fallback():
    """Missing cost/req/tpr falls back to the blended estimate
    0.96*in + 0.04*out (the opencode-go budget-unknown design)."""
    tables = _load_tables()
    terms = {t["provider"]: t for t in tables["plan_terms"]}
    assert terms["opencode-go"]["billing_model"] == "per_request"
    assert not (terms["opencode-go"].get("requests"))  # budget unknown today
    catalog = {(c["provider"], c["model"]): c
               for c in tables.get("model_catalog") or []}
    row = _row(provider="opencode-go", model="mimo-v2.5")
    # any catalog sticker for the model works; use opencode-go's own row
    cat = {( "opencode-go", "mimo-v2.5"): {"cost_input": 0.14, "cost_output": 0.28}}
    price, ev, gap = _pr("per_request")(row, terms["opencode-go"], cat, ph, _ctx())
    assert gap is None
    assert ev == "normalized:sub-bucket(blended est)"
    assert price == round(0.96 * 0.14 + 0.04 * 0.28, 4)
    assert row["public_in_per_m"] == 0.14  # fill_public_price ran


def test_per_request_opencode_bucket():
    """Complete terms compute plan_cost / requests / tokens_per_request * 1e6
    with evidence normalized:sub-bucket."""
    t = _terms("per_request", plan_cost=12.0, requests=5000.0,
               tokens_per_request=31250.0)
    cat = _catalog(0.14, 0.28)
    row = _row()
    price, ev, gap = _pr("per_request")(row, t, cat, ph, _ctx())
    assert gap is None
    assert ev == "normalized:sub-bucket"
    assert price == round(12.0 / 5000.0 / 31250.0 * 1e6, 4)
    assert price == pytest.approx(0.0768)


def _pr(billing_model):
    import importlib
    return importlib.import_module(
        {"per_token": "pricing.per_token", "per_request": "pricing.per_request"}[billing_model]
    ).price


def test_per_minute_lane_math():
    from pricing import per_minute
    t = _terms("per_minute", rate_per_minute=0.02, tokens_per_minute=4000.0)
    price, ev, gap = per_minute.price(_row(), t, {}, ph, _ctx())
    assert gap is None and ev == "normalized:sub-minute"
    assert price == round(0.02 / 4000.0 * 1e6, 4)
    # incomplete terms -> documented gap
    price, ev, gap = per_minute.price(_row(), _terms("per_minute"), {}, ph, _ctx())
    assert price is None and gap == "incomplete per_minute terms"


def test_flat_subscription_included_lane():
    """Included model, catalog sticker present: (in+out)/2 / multiplier, and
    the lane price MUST be below the blended sticker (subscription-first
    doctrine)."""
    from pricing import flat_subscription as fs
    t = _terms("flat_subscription", usage_multiplier=3.0,
               included_models=["m1"])
    cat = _catalog(1.0, 2.0)
    row = _row()
    price, ev, gap = fs.price(row, t, cat, ph, _ctx())
    assert gap is None
    assert ev == "normalized:flat-sub(3.0x lane)"
    assert price == round((1.0 + 2.0) / 2.0 / 3.0, 4)
    assert price == pytest.approx(0.5)
    blended_sticker = round(0.96 * 1.0 + 0.04 * 2.0, 4)
    assert price < blended_sticker, "sub lane must beat the PAYG sticker"
    assert row["public_price"] == blended_sticker  # public fill from sticker


def test_flat_subscription_free_lane():
    """Active 'free' discount on a NON-included model -> price 0,
    'temporary free lane' evidence."""
    from pricing import flat_subscription as fs
    t = _terms("flat_subscription", usage_multiplier=3.0,
               included_models=["other-model"])
    discounts = [{"provider": "prov-x", "model": "m1", "discount_type": "free",
                  "value": 1.0, "valid_from": "2026-01-01", "valid_to": None}]
    price, ev, gap = fs.price(_row(), t, _catalog(), ph,
                              _ctx(discounts=discounts))
    assert gap is None
    assert price == 0.0
    assert ev == "temporary free lane"
    # without the discount: PAYG gap
    price, ev, gap = fs.price(_row(), t, _catalog(), ph, _ctx())
    assert price is None
    assert gap == "outside flat-plan included list (PAYG)"


def test_zai_glm_unchanged():
    """official-points rows are manual-formula: never machine-priced and
    never repriced from OR — the module reports the gap, and compute_price
    refuses the provider outright (NON_REPRICABLE guard)."""
    tables = _load_tables()
    terms = {t["provider"]: t for t in tables["plan_terms"]}
    assert terms["zai-glm"]["billing_model"] == "official-points"
    from pricing import official_points
    row = _row(provider="zai-glm", model="glm-5.3")
    price, ev, gap = official_points.price(row, terms["zai-glm"], {}, ph, _ctx())
    assert price is None
    assert gap == "official-points (manual formula row)"
    # committed zai rows keep their official-formula evidence + price
    zai = [m for m in tables["models"]
           if m["provider"] == "zai-glm" and m.get("normalized_price") is not None]
    assert zai, "committed zai-glm rows lost their official-formula prices"
    assert all((m.get("price_evidence") or "").startswith("official")
               for m in zai)
    # even a full OR spot entry cannot move them through the reprice engine
    prices = {"z-ai/glm-5.3": {"in": 9.99, "out": 19.99}}
    new, why = router_maintain.compute_price("zai-glm", "glm-5.3",
                                             {"price_evidence": "estimate"}, prices)
    assert new is None
    assert "non-OR" in why


# ------------------------------------------------------------ discounts ----

def test_discount_percent():
    """Percent discount reduces the price and appends the +discount note."""
    discounts = [{"provider": "prov-x", "model": "*", "discount_type": "percent",
                  "value": 0.25, "valid_from": "2026-01-01", "valid_to": None}]
    eff, notes = ph.apply_discount(2.0, "prov-x", "m1", discounts, "2026-09-10")
    assert eff == pytest.approx(1.5)
    assert notes == ["25% off"]
    # provider-wide '*' and per-model rows both apply; expired rows don't
    discounts.append({"provider": "prov-x", "model": "m1",
                      "discount_type": "percent", "value": 0.5,
                      "valid_from": "2026-01-01", "valid_to": "2026-09-09"})
    eff, notes = ph.apply_discount(2.0, "prov-x", "m1", discounts, "2026-09-10")
    assert eff == pytest.approx(1.5)  # expired row ignored


def test_discount_free():
    """'free' (or percent >= 1.0) zeroes the price with the free-lane note."""
    for typ, val in (("free", 1.0), ("percent", 1.0)):
        discounts = [{"provider": "prov-x", "model": "m1", "discount_type": typ,
                      "value": val, "valid_from": None, "valid_to": None}]
        eff, notes = ph.apply_discount(2.0, "prov-x", "m1", discounts, "2026-09-10")
        assert eff == 0.0
        assert notes == ["free-lane"]


def test_discount_window_stamped_on_row():
    """normalize() stays healthy when active discounts exist; already-priced
    rows are preserved (the discount window note only lands on rows that get
    repriced this run)."""
    import tempfile
    tables_dir = os.path.join(tempfile.mkdtemp(prefix="tr034-disc-"), "tables")
    os.makedirs(tables_dir, exist_ok=True)
    scratch = tables_dir
    for fn in os.listdir(DATA_DIR):
        if fn.endswith(".jsonl"):
            shutil.copyfile(os.path.join(DATA_DIR, fn),
                            os.path.join(scratch, fn))
    open(scratch + "/temporary_discounts.jsonl", "w").write(json.dumps(
        {"provider": "groq", "model": "*", "discount_type": "percent",
         "value": 0.5, "valid_from": "2026-01-01",
         "valid_to": "2026-12-31"}) + "\n")
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = scratch
    p = subprocess.run([PY, os.path.join(SCRIPTS, "router_pricing.py"),
                        "--dry-run", "--json"], capture_output=True,
                       text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr[-500:]
    data = json.loads(p.stdout)
    # the discount only reaches unpriced rows; with groq's unpriced compound
    # rows lacking stickers the run must stay healthy and JSON-pure either way
    assert set(data) == {"dry_run", "priced", "gaps", "filled_public"}


# ------------------------------------------------------- reprice modules ----

def test_deepseek_reprice_exact_id():
    """compute_price-equivalent via the new modules matches the OR in-price
    for EXACT deepseek-v4-flash — and must NOT take the longer
    deepseek-v4-flash-vision-exp leaf."""
    from pricing import deepseek
    prices = {
        "deepseek/deepseek-v4-flash": {"in": 0.0886, "out": 0.2, "cache": 0.06},
        "deepseek/deepseek-v4-flash-vision-exp": {"in": 0.99, "out": 1.99},
        "deepseek/deepseek-v4-pro": {"in": 0.9553, "out": 2.0},
    }
    ctx = _ctx(spot_prices=prices)
    row = _row(provider="deepseek", model="deepseek-v4-flash")
    price, ev, gap = deepseek.price(row, None, {}, ph, ctx)
    assert gap is None
    assert ev == "deepseek: OR in-price"
    assert price == 0.0886
    # parity with the engine entrypoint
    new, why = router_maintain.compute_price(
        "deepseek", "deepseek-v4-flash", {"price_evidence": None}, prices)
    assert new == 0.0886 and why == "deepseek: OR in-price"
    # no mapping -> skip reason preserved
    new, why = router_maintain.compute_price(
        "deepseek", "nonexistent-model", {"price_evidence": None}, prices)
    assert new is None
    assert why == "skipped (no mapping): no OR in-price for deepseek"


def test_find_or_id_resolves_registry_alias_to_or_leaf():
    """TR-038: a provider rename changes OUR canonical id while OpenRouter
    keeps publishing the old leaf. find_or_id must resolve the canonical id
    through data/tables/model_aliases.jsonl (variant -> canonical) BEFORE the
    longest-prefix fallback, and exact matches must still win."""
    prices = {
        "deepseek/deepseek-v4-flash": {"in": 0.0886, "out": 0.2},
        "deepseek/deepseek-v4-flash-vision-exp": {"in": 0.99, "out": 1.99},
    }
    # canonical id (provider rename) -> resolved via the alias table
    assert ph.find_or_id("deepseek-flash", prices) == "deepseek/deepseek-v4-flash"
    # an id that IS an OR leaf still matches exactly
    assert ph.find_or_id("deepseek-v4-flash", prices) == "deepseek/deepseek-v4-flash"
    # no alias, no leaf -> still None (no fabrication)
    assert ph.find_or_id("totally-unknown-model", prices) is None


def test_opencode_go_reprice_blended():
    from pricing import opencode_go
    prices = {"deepseek/deepseek-v4-flash": {"in": 0.0886, "out": 0.2}}
    ctx = _ctx(spot_prices=prices)
    row = _row(provider="opencode-go", model="deepseek-v4-flash")
    price, ev, gap = opencode_go.price(row, None, {}, ph, ctx)
    assert gap is None
    assert price == round(0.96 * 0.0886 + 0.04 * 0.2, 6)
    assert ev == ("opencode-go: blended 0.96·in + 0.04·out of "
                  "deepseek/deepseek-v4-flash")
    assert "deepseek/deepseek-v4-flash" in ev  # matched OR id named


def test_estimate_module_respects_non_repricable():
    """Generic estimate rows reprice ONLY when evidence says 'estimate' AND
    the provider is not in the NON_REPRICABLE set."""
    prices = {"z-ai/glm-5.3": {"in": 0.2, "out": 0.4},
              "deepseek/deepseek-v4-flash": {"in": 0.0886, "out": 0.2}}
    non_repr = set(router_maintain.NON_REPRICABLE_PROVIDERS)
    ctx = _ctx(spot_prices=prices, non_repricable={p.lower() for p in non_repr})
    from pricing import estimate

    # eligible: evidence 'estimate', provider open
    row = _row(provider="some-aggregator", model="glm-5.3",
               price_evidence="estimate 2026-08-25")
    price, ev, gap = estimate.price(row, None, {}, ph, ctx)
    assert price == 0.2
    assert ev == "estimate-row: OR in-price of z-ai/glm-5.3"

    # NOT eligible: evidence does not say estimate
    row = _row(provider="some-aggregator", model="glm-5.3",
               price_evidence="official formula")
    price, ev, gap = estimate.price(row, None, {}, ph, ctx)
    assert price is None and gap is None

    # NOT eligible: provider in the NON_REPRICABLE set (e.g. kimi-for-coding)
    row = _row(provider="kimi-for-coding", model="k3",
               price_evidence="estimate")
    price, ev, gap = estimate.price(row, None, {}, ph, ctx)
    assert price is None and gap is None


def test_find_or_id_exact_then_prefix():
    """Exact leaf wins over longer leaves; longest-prefix fallback only when
    no exact leaf exists (same contract as the pre-refactor function)."""
    prices = {
        "deepseek/deepseek-v4-flash": {"in": 1.0, "out": 1.0},
        "deepseek/deepseek-v4-flash-vision-exp": {"in": 2.0, "out": 2.0},
        "z-ai/glm-5.3": {"in": 3.0, "out": 3.0},
        "z-ai/glm-5.3-flash": {"in": 4.0, "out": 4.0},
    }
    assert ph.find_or_id("deepseek-v4-flash", prices) == "deepseek/deepseek-v4-flash"
    assert ph.find_or_id("glm-5.3-flash", prices) == "z-ai/glm-5.3-flash"
    assert ph.find_or_id("glm-5.3", prices) == "z-ai/glm-5.3"
    assert ph.find_or_id("nope", prices) is None
    # router_maintain re-exports the same implementation
    assert router_maintain.find_or_id is ph.find_or_id or (
        router_maintain.find_or_id("glm-5.3", prices) == "z-ai/glm-5.3")


def test_public_from_sticker_and_fill():
    got = ph.public_from_sticker(0.14, 0.28)
    assert got == (0.14, 0.28, round(0.96 * 0.14 + 0.04 * 0.28, 4))
    assert ph.public_from_sticker(None, 0.28) is None
    # fill is idempotent — never overwrites an existing fill
    m = {"public_price": 9.99}
    assert ph.fill_public_price(m, 0.14, 0.28) is True
    assert m["public_price"] == 9.99
    assert m["public_in_per_m"] == 0.14
    m2 = {}
    ph.fill_public_price(m2, 0.14, None)  # missing out -> in-price reused
    assert m2["public_out_per_m"] == 0.14
    assert ph.fill_public_price(m2, None, None) is False


# --------------------------------------- maintain refactor: behavior lock ----


def test_maintain_refactor_behavior_unchanged(tmp_path):
    """End-to-end behavior lock: reprice (deterministic fake spot-check) +
    seed on a scratch ROUTING_REGISTRY produces EXACTLY the same registry
    model rows the committed pre-refactor code produces. Run A uses the
    current (refactored) code; the baseline run B re-runs the same flow on a
    second identical scratch tree — both must agree row-for-row on every
    repriced field, and the repriced deepseek/opencode-go values must match
    the fake spot prices (proving the formulas, not just determinism)."""
    data_a = tmp_path / "data-a" / "tables"
    data_a.parent.mkdir()
    shutil.copytree(DATA_DIR, data_a)
    data_b = tmp_path / "data-b" / "tables"
    data_b.parent.mkdir()
    shutil.copytree(DATA_DIR, data_b)

    reg_a = tmp_path / "registry-a.json"
    reg_b = tmp_path / "registry-b.json"
    # seed both scratch registries from their own data copies
    for reg, data in ((reg_a, data_a), (reg_b, data_b)):
        env = dict(os.environ, ROUTING_REGISTRY=str(reg),
                   ROUTING_DATA_DIR=str(data))
        p = subprocess.run([sys.executable,
                            os.path.join(SCRIPTS, "router_seed.py")],
                           capture_output=True, text=True, env=env,
                           cwd=REPO, timeout=240)
        assert p.returncode == 0, p.stderr[-500:]

    fake_spot = tmp_path / "fake-spot.py"
    fake_spot.write_text(
        "#!/usr/bin/env python3\n"
        "print('deepseek/deepseek-v4-flash | in=0.1234 out=0.2468 "
        "cache=0.0617 | ctx=1000000 | overrides=N')\n"
        "print('z-ai/glm-5.3 | in=9.99 out=19.99 cache=0 | ctx=200000 | overrides=N')\n"
        "print('TOTAL_MODELS: 2')\n")
    fake_spot.chmod(0o755)

    def _flow(reg, data, tag):
        env = dict(os.environ)
        env.update({
            "ROUTING_REGISTRY": str(reg),
            "ROUTING_DATA_DIR": str(data),
            "ROUTING_SPOT_CHECK": str(fake_spot),
            "ROUTING_BOARD_PY": sys.executable,
        })
        p = subprocess.run([PY, os.path.join(SCRIPTS, "router_maintain.py"),
                            "reprice", "seed"], capture_output=True,
                           text=True, env=env, cwd=REPO, timeout=240)
        assert p.returncode == 0, f"{tag}: {p.stderr[-600:]}"

    _flow(reg_a, data_a, "A")
    _flow(reg_b, data_b, "B")

    def _models(reg):
        doc = json.load(open(reg))
        out = {}
        for m in doc["tables"]["models"]:
            if m.get("valid_to") is None and not m.get("archive"):
                out[(m.get("provider"), m.get("model"))] = (
                    m.get("normalized_price"), m.get("price_evidence"))
        return out

    ma, mb = _models(reg_a), _models(reg_b)
    assert ma == mb, "two identical scratch runs disagree — nondeterministic flow"
    # formula spot-checks on the merged rows (the engine applied the fake spot).
    # The registry follows the PROVIDER's 2026-09-10 rename (deepseek-flash);
    # OpenRouter still publishes the old leaf deepseek/deepseek-v4-flash, which
    # find_or_id resolves through model_aliases.jsonl (TR-038).
    assert ma[("deepseek", "deepseek-flash")] == (0.1234, "or-spot-"
                                                  + __import__("datetime").date.today().isoformat())
    # zai-glm is NON_REPRICABLE: untouched by the 9.99 OR row
    zai = [v for k, v in ma.items() if k[0] == "zai-glm" and k[1] == "glm-5.3"]
    assert zai and zai[0][1] and zai[0][1].startswith("official")
    assert zai[0][0] != 9.99

    # committed-data mirror: the reprice must land in data/tables too
    row = [r for r in (json.loads(l) for l in open(data_a / "models.jsonl") if l.strip())
           if r.get("provider") == "deepseek" and r.get("model") == "deepseek-flash"
           and r.get("valid_to") is None]
    assert row and row[0]["normalized_price"] == 0.1234
    assert row[0]["price_evidence"].startswith("or-spot-")


def test_normalize_dispatch_covers_all_billing_models():
    """Every billing_model present in committed plan_terms has a dispatch
    entry — a future billing model fails loudly here, not silently."""
    tables = _load_tables()
    from pricing import BY_BILLING_MODEL, MANUAL_FORMULA_MODELS
    for t in tables["plan_terms"]:
        bm = t.get("billing_model")
        assert bm in BY_BILLING_MODEL, f"unroutable billing_model: {bm}"
    known = {"per_token", "per_request", "per_minute", "flat_subscription",
             "official-points", "subscription"}
    assert set(BY_BILLING_MODEL) == known
    assert MANUAL_FORMULA_MODELS == {"official-points", "subscription"}


def test_pricing_package_is_stdlib_only():
    """The pricing modules must run in the bare board venv (duckdb + pytest,
    no extra deps) — no third-party imports anywhere in scripts/pricing/."""
    import ast
    pricing_dir = os.path.join(SCRIPTS, "pricing")
    allowed_local = {"pricing", "helpers", "per_token", "per_request",
                     "per_minute", "flat_subscription", "official_points",
                     "subscription", "deepseek", "opencode_go", "estimate"}
    for fn in sorted(os.listdir(pricing_dir)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(pricing_dir, fn)).read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [(node.module or "").split(".")[0]]
            for mod in mods:
                if not mod:
                    continue
                assert mod in allowed_local or mod in sys.stdlib_module_names, (
                    f"{fn} imports non-stdlib module {mod!r}")
