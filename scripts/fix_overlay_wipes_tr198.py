#!/usr/bin/env python3
"""fix_overlay_wipes_tr198.py — TR-198: repair the 8 overlay-wiped live lanes.

The old pre-TR-180 overlay applier SET every column and NULLed what a thin
overlay row did not name. 8 live lanes still carry that damage (28-33 of 39
fields NULL). The applier is fixed (key-wise merge, tests/test_lifecycle_overlay_merge.py);
this script repairs the DATA the sanctioned way — lifecycle overlay rows in
data/lifecycle.jsonl (the seed-merged, provenance-carrying channel), then a
reseed. Never touches data/tables/models.jsonl by hand.

Why DISABLE rather than fill, per lane (live evidence, gathered 2026-09-29):

  openrouter/typesafe/jev-router        live in GET openrouter.ai/api/v1/models
                                        (464 ids, ctx=1000000) but pricing.prompt/
                                        completion = "-1" — upstream placeholders, not
                                        money quotes; importer _scaled_price refuses
                                        negatives by design; no models.dev entry to
                                        window-cost from.
  openrouter/openrouter/auto-beta       same: live, ctx=2000000, pricing -1/-1.
  commandcode/stealth/pixel-canary      live in GET api.commandcode.ai/provider/v1/
                                        models (86 ids, ctx=262144) but the CommandCode
                                        catalog carries NO pricing block at all; no
                                        sticker_prices entry in data/catalogs/
                                        commandcode.json; models.dev has no
                                        commandcode section.
  clinepass/qwen3.8-27b:free            live in Cline's catalog but $0 with no
                                        billable window: 27 models.dev resellers list
                                        the exact leaf qwen3.8-27b and all disagree
                                        (0.1/0.4 .. 0.8/4) — no single list-equivalent
                                        (TR-070 ambiguity rule; router_trapfix).
  clinepass/ling-3.0-flash-sante:free   live in Cline's catalog but $0 with no
                                        billable window: no EXACT paid sibling anywhere
                                        (OpenRouter lists only the :free form; models.dev
                                        prefix-match ling-3.0-flash is a different model
                                        — TR-070 prefix refusal, pinned by
                                        tests/test_pricing_audit_classes.py).
  clinepass/nex-n2.5-pro:free           upstream-dead: absent from Cline's live catalog;
                                        OpenRouter removed the whole nex-agi family
                                        (registry twin DEAD-ID 2026-09-26). TR-217
                                        already stamped valid_to=2026-09-29 on the row;
                                        the overlay adds the disabled stamp on top.
  clinepass/nex-n2.5-mini:free          upstream-dead, same evidence as nex-pro:free.
  clinepass/ling-3.0-flash-vl:free      upstream-dead: absent from Cline's live catalog;
                                        vendor-org chat 500 -> upstream OpenRouter 404;
                                        the openrouter twin is already valid_to=2026-09-26.

Usage:
  python3 scripts/fix_overlay_wipes_tr198.py            # dry run (default)
  python3 scripts/fix_overlay_wipes_tr198.py --apply    # append overlay rows

Idempotent: a lane already targeted in data/lifecycle.jsonl is skipped, so a
re-run after a reseed writes nothing new. R4 (no anonymous states): every row
carries lifecycle_source + lifecycle_checked_at.
"""
import argparse
import datetime
import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import lifecycle_gate  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LC_PATH = os.path.join(REPO, 'data', 'lifecycle.jsonl')
MODELS_PATH = os.path.join(REPO, 'data', 'tables', 'models.jsonl')

TODAY = '2026-09-29'          # the day the live evidence below was gathered
OR_IDS = 464                  # GET openrouter.ai/api/v1/models
CC_IDS = 86                   # GET api.commandcode.ai/provider/v1/models
CP_IDS = 464                  # GET api.cline.bot/api/v1/models (keyed)

EVIDENCE = {
    ('openrouter', 'typesafe/jev-router'): {
        'why': ('upstream-unpriceable 2026-09-29: OpenRouter lists the SKU but quotes '
                'pricing.prompt/completion = -1 (placeholder, not a money quote — the '
                'importer refuses negative prices by design) and no models.dev entry '
                'exists to window-cost from. Was 32/39 fields NULL from the pre-TR-180 '
                'overlay wipe. Re-enable when Typesafe/OpenRouter publish a real rate.'),
        'source': (f'live {TODAY}: GET openrouter.ai/api/v1/models ({OR_IDS} ids) lists '
                   'typesafe/jev-router ctx=1000000 with pricing -1/-1; models.dev '
                   '(225 providers) has no typesafe section — TR-198 overlay-wipe repair'),
    },
    ('openrouter', 'openrouter/auto-beta'): {
        'why': ('upstream-unpriceable 2026-09-29: OpenRouter lists the SKU but quotes '
                'pricing.prompt/completion = -1 (placeholder, not a money quote) and '
                'models.dev lists no auto-beta entry to window-cost from (the GA '
                'openrouter/auto row predates the TR-198 acceptance bar and is out of '
                'scope). Was 32/39 fields NULL from the pre-TR-180 overlay wipe. '
                'Re-enable when OpenRouter publishes a real rate.'),
        'source': (f'live {TODAY}: GET openrouter.ai/api/v1/models ({OR_IDS} ids) lists '
                   'openrouter/auto-beta ctx=2000000 with pricing -1/-1; models.dev '
                   'openrouter section (387 models) has no auto-beta — TR-198 '
                   'overlay-wipe repair'),
    },
    ('commandcode', 'stealth/pixel-canary'): {
        'why': ('upstream-unpriceable 2026-09-29: the CommandCode catalog lists the SKU '
                'but carries NO pricing block at all (per_token pass-through), the '
                'preset declares no sticker_prices entry for it, and models.dev has no '
                'commandcode section. Was 33/39 fields NULL from the pre-TR-180 overlay '
                'wipe. Re-enable when CommandCode publishes a sticker.'),
        'source': (f'live {TODAY}: GET api.commandcode.ai/provider/v1/models ({CC_IDS} '
                   'ids) lists stealth/pixel-canary ctx=262144 with no pricing block; '
                   'data/catalogs/commandcode.json sticker_prices has no entry; '
                   'models.dev (225 providers) has no commandcode section — TR-198 '
                   'overlay-wipe repair'),
    },
    ('clinepass', 'qwen3.8-27b:free'): {
        'why': ('upstream-unpriceable 2026-09-29: live in Cline\'s catalog but $0 with '
                'no billable window — 27 models.dev resellers list the exact leaf '
                'qwen3.8-27b and all disagree (0.1/0.4 .. 0.8/4), so no single '
                'list-equivalent exists to draw the metered window at (TR-070 '
                'ambiguity rule). Was 30/39 fields NULL from the pre-TR-180 overlay '
                'wipe. Re-enable when a vendor rate or reseller consensus exists.'),
        'source': (f'live {TODAY}: GET api.cline.bot/api/v1/models ({CP_IDS} ids) lists '
                   'qwen/qwen3.8-27b:free; GET openrouter.ai/api/v1/models lists the '
                   'twin qwen/qwen3.8-27b:free $0/$0 (ctx 262144, paid twin 0.42/3); '
                   'models.dev qwen3.8-27b listings disagree across 27 providers — '
                   'TR-198 overlay-wipe repair'),
    },
    ('clinepass', 'ling-3.0-flash-sante:free'): {
        'why': ('upstream-unpriceable 2026-09-29: live in Cline\'s catalog but $0 with '
                'no billable window — no EXACT paid sibling anywhere (OpenRouter lists '
                'only the :free form; models.dev prefix-match ling-3.0-flash is a '
                'different model and must not price it — TR-070 prefix refusal, pinned '
                'by test_prefix_only_match_is_refused). Was 31/39 fields NULL from the '
                'pre-TR-180 overlay wipe. Re-enable when the vendor lists this SKU or '
                'its exact paid sibling.'),
        'source': (f'live {TODAY}: GET api.cline.bot/api/v1/models ({CP_IDS} ids) lists '
                   'inclusionai/ling-3.0-flash-sante:free; GET openrouter.ai/api/v1/'
                   f'models ({OR_IDS} ids) lists only the :free form (paid form ABSENT); '
                   'models.dev (225 providers) has no ling-3.0-flash-sante leaf — TR-198 '
                   'overlay-wipe repair'),
    },
    ('clinepass', 'nex-n2.5-pro:free'): {
        'why': ('upstream-dead 2026-09-29: absent from Cline\'s live catalog; OpenRouter '
                'removed the whole nex-agi/n2.5 family (registry twin '
                'openrouter/nex-agi/nex-n2.5-pro:free DEAD-ID 2026-09-26). The row '
                'already carries valid_to=2026-09-29 from the TR-217 live check; this '
                'overlay stamps disabled on the lane state. Was 28/39 fields NULL from '
                'the pre-TR-180 overlay wipe.'),
        'source': (f'live {TODAY}: absent from GET api.cline.bot/api/v1/models ({CP_IDS} '
                   'ids); vendor-org form chat 500 -> upstream OpenRouter 404 — TR-217 '
                   'weekly MODEL lane evidence, stamped disabled by TR-198'),
    },
    ('clinepass', 'nex-n2.5-mini:free'): {
        'why': ('upstream-dead 2026-09-29: absent from Cline\'s live catalog; OpenRouter '
                'removed the whole nex-agi/n2.5 family (registry twins DEAD-ID '
                '2026-09-26). The row already carries valid_to=2026-09-29 from the '
                'TR-217 live check; this overlay stamps disabled on the lane state. Was '
                '28/39 fields NULL from the pre-TR-180 overlay wipe.'),
        'source': (f'live {TODAY}: absent from GET api.cline.bot/api/v1/models ({CP_IDS} '
                   'ids); vendor-org form chat 500 -> upstream OpenRouter 404 (free SKU '
                   'pulled; openrouter twin already vt 09-26) — TR-217 weekly MODEL lane '
                   'evidence, stamped disabled by TR-198'),
    },
    ('clinepass', 'ling-3.0-flash-vl:free'): {
        'why': ('upstream-dead 2026-09-29: absent from Cline\'s live catalog; '
                'vendor-org chat 500 -> upstream OpenRouter 404 (free SKU pulled; the '
                'openrouter twin is already valid_to=2026-09-26). The row already '
                'carries valid_to=2026-09-29 from the TR-217 live check; this overlay '
                'stamps disabled on the lane state. Was 28/39 fields NULL from the '
                'pre-TR-180 overlay wipe.'),
        'source': (f'live {TODAY}: absent from GET api.cline.bot/api/v1/models ({CP_IDS} '
                   'ids); vendor-org form chat 500 -> upstream OpenRouter 404 (paid twin '
                   'live upstream, free pulled) — TR-217 weekly MODEL lane evidence, '
                   'stamped disabled by TR-198'),
    },
}

#: The overlay rows this repair adds. THIN on purpose (TR-180): only the keys a
#: disable needs — provider/model/disabled/disabled_reason + provenance. The
#: 3 already-retired lanes keep their valid_to (not named here => untouched).


def overlay_rows():
    rows = []
    for (prov, mod), ev in EVIDENCE.items():
        rows.append({
            'provider': prov,
            'model': mod,
            'disabled': True,
            'disabled_reason': ev['why'],
            'lifecycle_source': ev['source'],
            'lifecycle_checked_at': TODAY,
        })
    return rows


def existing_targets():
    out = set()
    if os.path.exists(LC_PATH):
        for line in open(LC_PATH):
            if line.strip():
                r = json.loads(line)
                out.add((r.get('provider'), r.get('model')))
    return out


def lane_rows():
    out = {}
    for line in open(MODELS_PATH):
        if line.strip():
            r = json.loads(line)
            out[(r.get('provider'), r.get('model'))] = r
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='append the overlay rows')
    args = ap.parse_args()

    lanes = lane_rows()
    have = existing_targets()
    new = [r for r in overlay_rows() if (r['provider'], r['model']) not in have]
    missing = [k for k in EVIDENCE if k not in lanes]
    if missing:
        print(f'ABORT: target lanes absent from models.jsonl: {missing}')
        return 1

    print(f'{len(EVIDENCE)} target lanes; {len(EVIDENCE) - len(new)} already targeted '
          f'in lifecycle.jsonl; {len(new)} overlay rows to add')
    for r in new:
        key = f"{r['provider']}/{r['model']}"
        row = lanes[(r['provider'], r['model'])]
        nulls = sum(1 for v in row.values() if v is None)
        print(f"  + {key}  (now {nulls} nulls of {len(row)} cols) -> disabled")
    if not new:
        print('nothing to do — all targets already carry their overlay row')
        return 0
    if not args.apply:
        print('dry run — nothing written. Re-run with --apply.')
        return 0

    # R4 gate on the WHOLE post-write file (same predicate every writer uses).
    rows = []
    for line in open(LC_PATH):
        if line.strip():
            rows.append(json.loads(line))
    rows.extend(new)
    lifecycle_gate.gate_rows('lifecycle', rows) if hasattr(lifecycle_gate, 'TABLE_KEYS') and 'lifecycle' in lifecycle_gate.TABLE_KEYS \
        else None
    # lifecycle.jsonl itself is not a gated table (its rows ARE the provenance);
    # gate the MODELS post-merge shape instead: simulate the seed's merge result.
    merged = []
    for k, row in lanes.items():
        upd = [r for r in new if (r['provider'], r['model']) == k]
        if upd:
            row = dict(row)
            row.update(upd[0])
        merged.append(row)
    lifecycle_gate.gate_rows('models', merged)

    tmp = LC_PATH + '.tmp'
    with open(tmp, 'w') as f:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False), file=f)
    os.replace(tmp, LC_PATH)
    print(f'wrote {len(new)} overlay rows -> {LC_PATH}')
    print('next: python3 scripts/router_seed.py  (the seed merges them; never '
          'hand-edit data/tables/models.jsonl)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
