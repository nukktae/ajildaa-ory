#!/usr/bin/env python3
"""
jasoseol.com scraper.

Two phases:
  1. LIST   GET /search?...&page=N  -> __NEXT_DATA__ .dehydratedState (react-query) -> postings + totalCount
  2. DETAIL GET /recruit/{id}       -> __NEXT_DATA__ .pageProps.initialEmploymentCompany

Outputs (in --out, default ./data):
  postings.jsonl  one merged record per posting (list fields + detail fields)
  postings.csv    flat one-row-per-posting summary
  roles.csv       one row per 직무 (employments[])
  images/         posting body images, if --images

Notes:
  - The posting body (`content`) is usually a single <img>; there is no structured
    requirements text. Image URLs are extracted into `content_image_urls`.
  - Reruns are resumable: ids already in postings.jsonl are skipped unless --refresh.
  - --keep-history retains postings that have dropped out of the search results
    (closed/expired), so the archive only ever grows. New postings get a
    first_seen date and are appended to --new-log.
"""

import argparse, csv, json, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

BASE = "https://jasoseol.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")
KST = timezone(timedelta(hours=9))

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, file=sys.stderr, flush=True)


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def get_next_data(session, url, tries=4, delay=1.0):
    """GET a Next.js page and return its parsed __NEXT_DATA__."""
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code}")
            r.raise_for_status()
            m = NEXT_RE.search(r.text)
            if not m:
                raise ValueError("no __NEXT_DATA__ in response")
            return json.loads(m.group(1))
        except Exception as e:  # noqa: BLE001
            last = e
            sleep = delay * (2 ** i) + random.uniform(0, 0.4)
            log(f"  retry {i+1}/{tries} {url} ({e}) in {sleep:.1f}s")
            time.sleep(sleep)
    log(f"  FAILED {url}: {last}")
    return None


# ---------------------------------------------------------------- list phase

class ListPageError(RuntimeError):
    """A search page could not be parsed after retries."""


def parse_list_page(data):
    """Pull {data, totalCount} out of a /search page's __NEXT_DATA__.

    The site intermittently server-renders /search with an empty react-query
    cache; that yields no rows and must be retried, not treated as 'no results'.
    """
    queries = (data or {}).get("props", {}).get("pageProps", {}) \
        .get("dehydratedState", {}).get("queries") or []
    q = next((x for x in queries if (x.get("queryKey") or [None])[0] == "jobSearch"), None)
    if q is None:
        raise ListPageError("no jobSearch query in SSR payload")
    d = (q.get("state") or {}).get("data") or {}
    if "data" not in d:
        raise ListPageError("jobSearch query has no data")
    return d["data"], d.get("totalCount")


def fetch_list_page(session, filters, page, per_page, tries=4, delay=2.0):
    params = dict(filters)
    params.update({"page": page, "perPage": per_page})
    url = f"{BASE}/search?{urlencode(params, doseq=True)}"
    for i in range(tries):
        try:
            return parse_list_page(get_next_data(session, url))
        except ListPageError as e:
            if i == tries - 1:
                raise ListPageError(f"page {page}: {e}") from e
            sleep = delay * (2 ** i) + random.uniform(0, 0.5)
            log(f"  empty SSR payload on list page {page} ({e}); retry in {sleep:.1f}s")
            time.sleep(sleep)


def fetch_all_listings(session, filters, per_page, delay, limit=None):
    rows, seen = [], set()
    page = 1
    total = None
    while True:
        items, total = fetch_list_page(session, filters, page, per_page)
        if not items:
            break
        new = [it for it in items if it["id"] not in seen]
        for it in new:
            seen.add(it["id"])
        rows.extend(new)
        log(f"list page {page}: +{len(new)} (have {len(rows)}"
            + (f"/{total}" if total else "") + ")")
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        if total is not None and len(rows) >= total:
            break
        if len(items) < per_page:
            break
        page += 1
        time.sleep(delay)
    return rows, total


# -------------------------------------------------------------- detail phase

def fetch_detail(session, posting_id, delay):
    time.sleep(random.uniform(0, delay))
    data = get_next_data(session, f"{BASE}/recruit/{posting_id}")
    if not data:
        return None
    return data["props"]["pageProps"].get("initialEmploymentCompany")


def merge(listing, detail):
    rec = dict(listing)
    if detail:
        for k, v in detail.items():
            if k == "ret":
                continue
            # detail wins, but never overwrite a value with null
            if v is not None or k not in rec:
                rec[k] = v
    content = rec.get("content") or ""
    rec["content_image_urls"] = IMG_SRC_RE.findall(content)
    rec["content_text"] = re.sub(r"\s+", " ", TAG_RE.sub(" ", content)).strip()
    rec["url"] = f"{BASE}/recruit/{rec['id']}"
    return rec


# ------------------------------------------------------------------- outputs

POSTING_COLS = [
    "id", "url", "name", "title", "business_size", "employment_page_url",
    "start_time", "end_time", "recruit_type", "target", "direct_apply",
    "is_receive_applicant", "view_count", "favorite_count", "homepage_count",
    "resumes_count", "company_group_id", "chat_id", "role_count", "roles",
    "image_url", "content_image_count", "content_image_urls",
    "attached_file_url", "created_at", "opened_at", "first_seen",
]


def write_outputs(records, out_dir):
    records = sorted(records, key=lambda r: r["id"])
    with open(os.path.join(out_dir, "postings.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(os.path.join(out_dir, "postings.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POSTING_COLS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            emps = r.get("employments") or []
            cg = r.get("company_group") or {}
            w.writerow({
                **r,
                "business_size": cg.get("business_size"),
                "company_group_id": r.get("company_group_id") or cg.get("id"),
                "role_count": len(emps),
                "roles": " | ".join(e.get("field") or "" for e in emps),
                "content_image_count": len(r.get("content_image_urls") or []),
                "content_image_urls": " | ".join(r.get("content_image_urls") or []),
            })

    with open(os.path.join(out_dir, "roles.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["posting_id", "company", "posting_title", "end_time",
                    "employment_id", "field", "division", "resume_count",
                    "employment_resume", "duty_group_ids"])
        for r in records:
            for e in (r.get("employments") or []):
                div = e.get("division")
                w.writerow([
                    r["id"], r.get("name"), r.get("title"), r.get("end_time"),
                    e.get("id"), e.get("field"),
                    ",".join(map(str, div)) if isinstance(div, list) else div,
                    e.get("resume_count"), e.get("employment_resume"),
                    ",".join(map(str, e.get("duty_group_ids") or [])),
                ])


def download_images(session, records, out_dir, workers, delay):
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    jobs = [(r["id"], i, u) for r in records
            for i, u in enumerate(r.get("content_image_urls") or [])]

    def one(job):
        pid, idx, url = job
        ext = os.path.splitext(url.split("?")[0])[1] or ".img"
        path = os.path.join(img_dir, f"{pid}_{idx}{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        time.sleep(random.uniform(0, delay))
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        except Exception as e:  # noqa: BLE001
            log(f"  image FAILED {url}: {e}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, jobs))
    log(f"images: {len(jobs)} referenced -> {img_dir}")


def append_new_log(path, new_records, today):
    """Append one row per newly discovered posting to a running CSV log."""
    if not new_records:
        return
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["first_seen", "id", "company", "title", "start_time",
                        "end_time", "role_count", "url"])
        for r in sorted(new_records, key=lambda x: x["id"]):
            w.writerow([today, r["id"], r.get("name"), r.get("title"),
                        r.get("start_time"), r.get("end_time"),
                        len(r.get("employments") or []), r.get("url")])


# ---------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Scrape jasoseol.com job postings.")
    p.add_argument("--division", default="1",
                   help="1=신입, 2=경력 ... comma-separated; empty for all")
    p.add_argument("--business-types", default="big_business",
                   help="e.g. big_business,middle_business,public_institution; empty for all")
    p.add_argument("--keyword", default="")
    p.add_argument("--duty-group-ids", default="")
    p.add_argument("--exclude-closed", action="store_true", help="drop already-closed postings")
    p.add_argument("--per-page", type=int, default=100)
    p.add_argument("--limit", type=int, help="stop after N postings (testing)")
    p.add_argument("--workers", type=int, default=4, help="concurrent detail fetches")
    p.add_argument("--delay", type=float, default=0.5, help="politeness delay (s)")
    p.add_argument("--out", default="data")
    p.add_argument("--no-detail", action="store_true", help="list pages only")
    p.add_argument("--images", action="store_true", help="download posting body images")
    p.add_argument("--refresh", action="store_true", help="re-fetch details already cached")
    p.add_argument("--keep-history", action="store_true",
                   help="retain cached postings that no longer appear in search results")
    p.add_argument("--new-log", help="append newly discovered postings to this CSV")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    session = make_session()

    filters = {}
    if args.division:
        filters["division"] = [d for d in args.division.split(",") if d]
    if args.business_types:
        filters["businessTypes"] = [b for b in args.business_types.split(",") if b]
    if args.duty_group_ids:
        filters["dutyGroupIds"] = [d for d in args.duty_group_ids.split(",") if d]
    if args.keyword:
        filters["keyword"] = args.keyword
    if args.exclude_closed:
        filters["excludeClosed"] = "true"

    log(f"filters: {filters}")
    listings, total = fetch_all_listings(session, filters, args.per_page, args.delay, args.limit)
    log(f"listings: {len(listings)}" + (f" (site reports totalCount={total})" if total else ""))
    if total and len(listings) < total and not args.limit:
        log(f"ABORT: got {len(listings)} listings but site reported {total}; "
            "refusing to write a truncated dataset")
        return 1

    cache = {}
    cache_path = os.path.join(args.out, "postings.jsonl")
    if os.path.exists(cache_path) and not args.refresh:
        with open(cache_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    cache[r["id"]] = r
                except json.JSONDecodeError:
                    pass
        log(f"cache: {len(cache)} postings already fetched")

    if args.no_detail:
        records = [merge(l, None) for l in listings]
    else:
        todo = [l for l in listings if l["id"] not in cache]
        log(f"details to fetch: {len(todo)}")
        done = [0]

        def work(listing):
            d = fetch_detail(session, listing["id"], args.delay)
            done[0] += 1
            if done[0] % 25 == 0:
                log(f"  details {done[0]}/{len(todo)}")
            return merge(listing, d)

        fetched = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            fetched = list(ex.map(work, todo))
        records = fetched + [cache[l["id"]] for l in listings if l["id"] in cache]

    today = datetime.now(KST).date().isoformat()
    listed_ids = {l["id"] for l in listings}
    new_records = [r for r in records if r["id"] not in cache]
    for r in records:
        r.setdefault("first_seen", today)

    if args.keep_history:
        stale = [r for r in cache.values() if r["id"] not in listed_ids]
        if stale:
            log(f"keeping {len(stale)} archived postings no longer in search results")
        records = records + stale

    write_outputs(records, args.out)
    log(f"wrote {len(records)} postings to {args.out}/postings.{{jsonl,csv}} and roles.csv")
    log(f"NEW today: {len(new_records)}")
    for r in sorted(new_records, key=lambda x: x["id"]):
        log(f"  + {r['id']} {r.get('name')} — {r.get('title')} (~{r.get('end_time','')[:10]})")

    if args.new_log:
        append_new_log(args.new_log, new_records, today)

    if args.images:
        download_images(session, records, args.out, args.workers, args.delay)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except ListPageError as e:
        log(f"ABORT: {e}")
        sys.exit(1)
