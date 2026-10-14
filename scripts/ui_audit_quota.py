#!/usr/bin/env python3
"""TR-212 — screenshot audit for the quota remaining panel.

Follows the TR-193 audit rule: capture + VERIFY BEFORE STORING. The run fails
loudly when the page is foreign (title mismatch), the quota panel rendered no
rows, or the honesty forms are missing from the DOM. Artifacts land under
docs/ui/ as <date>-quota-*.png plus a JSON manifest recording every check.

Usage:
  python3 scripts/ui_audit_quota.py --base-url http://127.0.0.1:9193 \
      [--output-dir docs/ui] [--json]
"""
import argparse
import asyncio
import datetime
import glob
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

EXPECTED_TITLE = "task-router — data command center"


def _find_chromium():
    """The newest installed playwright chromium build (the driver's expected
    build number can drift ahead of the installed browser)."""
    cands = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    if not cands:
        raise SystemExit("no chromium found under ~/.cache/ms-playwright — "
                         "run `playwright install chromium` or set TR212_CHROME")
    return cands[-1]

CHECKS_JS = """
() => {
  const $ = (s) => document.querySelector(s);
  const txt = (s) => { const el = $(s); return el ? el.textContent : ''; };
  const qRows = document.querySelectorAll('#quota table tbody tr').length;
  const bRows = document.querySelectorAll('#burn table tbody tr').length;
  const badges = {
    est: document.querySelectorAll('#quota .warn, #burn .warn').length,
    observed: Array.from(document.querySelectorAll('#quota span.ok'))
      .filter(el => el.textContent.trim() === 'OBSERVED').length,
  };
  return {
    title: document.title,
    quota_rows: qRows,
    quota_note: txt('#quota_note'),
    burn_rows: bRows,
    burn_note: txt('#burn_note'),
    has_unknown_form: /unknown \\(/.test(txt('#quota')),
    has_est_badge: txt('#quota').includes('EST') || bRows > 0,
    has_countdown: bRows === 0 || /\\d+[smhd]/.test(txt('#burn')),
    badges,
  };
}
"""


async def audit(base_url: str, out_dir: Path) -> dict:
    from playwright.async_api import async_playwright
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"audit_timestamp": datetime.datetime.now(
                    datetime.timezone.utc).isoformat() + "Z",
                "base_url": base_url, "checks": {}, "artifacts": [],
                "status": "fail"}

    async with async_playwright() as p:
        exe = os.environ.get("TR212_CHROME") or _find_chromium()
        browser = await p.chromium.launch(
            executable_path=exe, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1680, "height": 1050})
        await page.goto(f"{base_url}/ui", wait_until="networkidle",
                        timeout=30000)
        await page.wait_for_timeout(1200)

        c = await page.evaluate(CHECKS_JS)
        manifest["checks"] = c

        failures = []
        if c["title"] != EXPECTED_TITLE:
            failures.append(f"foreign page: title={c['title']!r}")
        if c["quota_rows"] < 1:
            failures.append(f"quota panel has {c['quota_rows']} rows — a "
                            f"panel that should have rows rendered none")
        if not c["has_unknown_form"]:
            failures.append("no 'unknown (...)' NULL rendering found in the "
                            "quota panel")
        if not c["has_est_badge"]:
            failures.append("no EST/observed rung marking found")
        if c["quota_rows"] and not c["quota_note"].strip():
            failures.append("quota panel rendered no provenance note")

        # --- shots (only stored if checks passed) -------------------------
        if not failures:
            quota_sec = page.locator("section", has=page.locator("#quota"))
            await quota_sec.screenshot(
                path=str(out_dir / f"{date}-quota-panel.png"))
            manifest["artifacts"].append(f"{date}-quota-panel.png")
            # the OBSERVED half of the ladder: scroll the first rung-2 row
            # into view so the evidence shows both rungs, not only estimates
            obs = page.locator("#quota tr", has_text="OBSERVED").first
            if await obs.count():
                await obs.scroll_into_view_if_needed()
                await page.evaluate(
                    "document.querySelector('#quota').scrollTop -= 60")
                await page.wait_for_timeout(300)
                await quota_sec.screenshot(
                    path=str(out_dir / f"{date}-quota-observed.png"))
                manifest["artifacts"].append(f"{date}-quota-observed.png")
            if c["burn_rows"]:
                burn_el = page.locator("#burn")
                await burn_el.scroll_into_view_if_needed()
                await page.screenshot(
                    path=str(out_dir / f"{date}-quota-burn.png"))
                manifest["artifacts"].append(f"{date}-quota-burn.png")
            await page.screenshot(
                path=str(out_dir / f"{date}-command-center-quota.png"),
                full_page=True)
            manifest["artifacts"].append(f"{date}-command-center-quota.png")
            manifest["status"] = "pass"
        else:
            manifest["failures"] = failures
        await browser.close()

    (out_dir / f"{date}-quota-manifest.json").write_text(
        json.dumps(manifest, indent=2))
    manifest["manifest"] = f"{date}-quota-manifest.json"
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:9193")
    ap.add_argument("--output-dir", default=str(REPO / "docs" / "ui"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    m = asyncio.run(audit(a.base_url, Path(a.output_dir)))
    if a.json:
        print(json.dumps(m, indent=2))
    else:
        print(f"audit {m['status']}: quota_rows="
              f"{m['checks'].get('quota_rows')} burn_rows="
              f"{m['checks'].get('burn_rows')} artifacts={m['artifacts']}")
        for f in m.get("failures", []):
            print(f"FAIL: {f}")
    return 0 if m["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
