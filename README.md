# jasoseol scraper

Pulls job postings from jasoseol.com (자소설닷컴).

## Run
    .venv/bin/python scrape.py --out data                 # 대기업 + 신입 (580 postings)
    .venv/bin/python scrape.py --business-types "" --division ""   # everything
    .venv/bin/python scrape.py --keyword 반도체 --exclude-closed
    .venv/bin/python scrape.py --out data --images        # also download body images

Reruns are resumable: ids already in `data/postings.jsonl` are skipped (`--refresh` to force).

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

## Caveat
The posting body is an image, not text: 548 of 580 postings have `content` = a single
`<img>` with no readable requirements. `content_image_urls` holds those URLs; run with
`--images` and OCR them if you need qualifications / process / location.
