# Policy Radar — 0 Kč pilot

A deliberately simple CZ/EU legislative monitoring pilot that can run on **GitHub Free + GitHub Pages** with no paid database, server or AI API.

## What it does

1. Once per weekday GitHub Actions runs `collector/run.py`.
2. The collector reads public official RSS/pages.
3. It fingerprints items and marks them `NEW`, `CHANGED` or `UNCHANGED`.
4. A rule-based matcher compares titles/descriptions with priorities in `config/priorities.json`.
5. It writes `docs/data/radar.json`.
6. GitHub Pages serves `docs/index.html` as the dashboard.

No database is required in phase 1. Git history itself gives us an audit trail of daily changes.

## Public sources currently wired

- EUR-Lex — Commission proposals RSS
- EUR-Lex — Parliament & Council legislation RSS
- Council public register — latest documents RSS
- Council — Coreper RSS
- Council — Competitiveness working parties RSS
- European Parliament ITRE RSS
- VeKLEP public material list (tolerant HTML parser; this source may require parser maintenance if ODok changes markup)

The dashboard ships with **demo data** so you can open it before the first live collection.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python collector/run.py
python -m http.server 8000 -d docs
```

Open http://localhost:8000

## Deploy for 0 Kč on GitHub Pages

1. In **Settings → Pages**, choose **Deploy from a branch**.
2. Branch: `main`; folder: `/docs`.
3. In **Actions**, run `Daily Policy Radar` manually once.
4. GitHub Pages will provide the live URL.

For the zero-cost pilot, keep only non-confidential priorities in `config/priorities.json`.

## Change tracked priorities

Edit `config/priorities.json`. Each priority has:
- `id`
- `owner`
- `topic`
- `position`
- `keywords`
- `weight` (1–5)

The free pilot uses deterministic keyword scoring. It intentionally does **not** make substantive AI judgments.

## Pilot architecture

`official public sources → GitHub Action → JSON → static dashboard`

Later we can migrate `JSON → Supabase/Postgres` and add AI assessment, article-level diffs, confidential member priorities, authentication, digests and more sources.

## Important limitations

- RSS summaries are not full legislative texts.
- `CHANGED` reflects a changed feed item payload; article-level legislative diff is phase 2.
- VeKLEP HTML parsing is best-effort and source health is visible in the UI.
- GitHub scheduled workflows are best-effort rather than exact-time schedulers.
- The free phase should not contain confidential member positions.
