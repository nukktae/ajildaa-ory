# jasoseol scraper

Pulls job postings from jasoseol.com (자소설닷컴).

## Run
    .venv/bin/python scrape.py --out data                 # 대기업 + 신입 (580 postings)
    .venv/bin/python scrape.py --business-types "" --division ""   # everything
    .venv/bin/python scrape.py --keyword 반도체 --exclude-closed
    .venv/bin/python scrape.py --out data --images        # also download body images

Reruns are resumable: ids already in `data/postings.jsonl` are skipped (`--refresh` to force).

## Browse locally

    python serve.py            # http://localhost:8000
    python serve.py --port 9000

Stdlib only, no dependencies. Serves `web/index.html` plus `/api/postings.json`
built from `data/postings.jsonl` (reloaded automatically when the file changes).
The whole dataset ships once, so search / filter / sort are instant client-side —
no request per keystroke (Doherty threshold, <400ms).

UI: opens on **Open postings only** (closed ones are archive, not action — reach them
via the All / Closed chips). Search across company + posting + role, status chips, sort by deadline / recency / views / role count, a result count that is always
visible (including zero), one-click filter reset, and an empty state that offers a way out.
White background, no borders anywhere — grouping comes from fills and spacing, and focus
is shown by a fill change rather than a ring.

## Daily automation

`.github/workflows/daily-scrape.yml` runs at **06:10 KST every day** (`10 21 * * *` UTC)
and can also be triggered from the Actions tab. Each run:

1. scrapes with the same filters (`--division 1 --business-types big_business`),
2. fetches details only for postings it has never seen,
3. keeps postings that have since closed and dropped out of search (`--keep-history`),
4. appends new ones to `data/new-postings.csv` with a `first_seen` date,
5. commits `data/` back to this repo, and lists the new postings in the run summary.

`data/` is committed on purpose — `postings.jsonl` **is** the run-to-run state. Because
cached postings are never re-fetched, daily diffs are just the added lines.

To see what arrived on a given day: `grep ^2026-09-01 data/new-postings.csv`,
or read the Actions run summary.

## How it works
No public search API — `/api/v1/employment_companies` ignores the filter params.
Instead both phases parse the SSR `__NEXT_DATA__` blob:

1. **List** `GET /search?division=1&businessTypes=big_business&page=N&perPage=100`
   → `props.pageProps.dehydratedState.queries[jobSearch].state.data`
   → `{data[], page, perPage, totalCount}`. 100/page is honored; 6 requests for 580.
2. **Detail** `GET /recruit/{id}`
   → `props.pageProps.initialEmploymentCompany` (company_group, employments[], content HTML, counts).

## Outputs
- `postings.jsonl` — full merged record per posting
- `postings.csv` — one row per posting (roles joined with `|`)
- `roles.csv` — one row per 직무 (5,617 rows for the 580 postings)
- `images/` — posting body images, with `--images`

## Failure modes handled
- `/search` intermittently server-renders with an empty react-query cache. That is
  retried (4x, exponential backoff); if a page still can't be parsed, the run aborts
  non-zero rather than committing a truncated dataset.
- A short listing run (fewer rows than the site's own `totalCount`) also aborts.
- 429/5xx on detail pages retry with backoff; a posting that still fails is skipped
  and simply retried on the next daily run.

## Caveat
The posting body is an image, not text: 548 of 580 postings have `content` = a single
`<img>` with no readable requirements. `content_image_urls` holds those URLs; run with
`--images` and OCR them if you need qualifications / process / location.
