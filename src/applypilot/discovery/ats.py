"""Company career boards via public ATS APIs: Greenhouse, Lever, Ashby, Keka.

These boards publish their open roles as JSON without auth, so discovery is
fast, needs no scraping, and the apply URL goes straight to the ATS form the
agent knows how to fill.

Configured in searches.yaml:

    ats_boards:
      greenhouse: [razorpay, groww]
      lever: [cred]
      ashby: [sarvam]
      keka: [spyneai]
    # or point at files with the same lists (e.g. job-apply-agent's sources.yaml)
    ats_sources_file: C:/path/to/sources.yaml
    ats_sources_files: [C:/path/to/more.yaml]
    max_experience_years: 3   # drop roles whose minimum experience is above this

Titles must contain one of ``title_keywords`` (falls back to the search
``queries``), must not contain ``exclude_titles``, and the location must pass
the same accept/reject filter as the other sources.
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse sends it) to plain text."""
    if not raw:
        return ""
    s = html.unescape(html.unescape(raw))
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = re.sub(r"[^\S\n]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _greenhouse(client: httpx.Client, board: str) -> list[dict]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        jid = j.get("id")
        out.append({
            "url": j.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
            # The embed form never redirects to the company's own careers page.
            "application_url": f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={jid}",
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "description": _text(j.get("content")),
            "company": j.get("company_name") or board,
            "posted": j.get("first_published") or j.get("updated_at"),
        })
    return out


def _lever(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} (Remote)" if loc else "Remote"
        hosted = j.get("hostedUrl", "")
        desc = j.get("descriptionPlain") or _text(j.get("description"))
        for block in j.get("lists") or []:
            desc += f"\n\n{block.get('text', '')}\n{_text(block.get('content'))}"
        out.append({
            "url": hosted,
            "application_url": j.get("applyUrl") or (f"{hosted}/apply" if hosted else None),
            "title": j.get("text", "").strip(),
            "location": loc,
            "description": desc.strip(),
            "company": slug,
            "posted": j.get("createdAt"),
        })
    return out


def _ashby(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                   params={"includeCompensation": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "")
        extra = [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if extra:
            loc = ", ".join([loc] + extra)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)" if loc else "Remote"
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append({
            "url": j.get("jobUrl", ""),
            "application_url": j.get("applyUrl") or j.get("jobUrl"),
            "title": j.get("title", "").strip(),
            "location": loc,
            "description": j.get("descriptionPlain") or _text(j.get("descriptionHtml")),
            "company": slug,
            "salary": comp,
            "posted": j.get("publishedAt"),
        })
    return out


def _keka(client: httpx.Client, slug: str) -> list[dict]:
    base = f"https://{slug}.keka.com/careers"
    r = client.get(f"{base}/api/jobs/default/active")
    r.raise_for_status()
    out = []
    for j in r.json():
        locs = j.get("jobLocations") or []
        loc = "; ".join(", ".join(x for x in (l.get("city"), l.get("countryName")) if x) for l in locs)
        url = f"{base}/jobdetails/{j.get('id')}"
        out.append({
            "url": url,
            "application_url": url,
            "title": (j.get("title") or "").strip(),
            "location": loc or "India",
            "description": _text(j.get("description")),
            "company": slug,
            "salary": j.get("salaryRangeFormat"),
            "experience": j.get("experience"),
            "posted": j.get("publishedOn"),
        })
    return out


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "keka": _keka}

_YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2}|to\s*\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)


def _min_experience(j: dict) -> int | None:
    """Minimum years asked for: Keka's experience field, else the first 'N years' in the text."""
    exp = str(j.get("experience") or "")
    m = re.match(r"\s*(\d{1,2})", exp)
    if m:
        return int(m.group(1))
    m = _YEARS_RE.search(j.get("description") or "")
    return int(m.group(1)) if m else None


def _load_boards(cfg: dict) -> dict[str, list[str]]:
    boards: dict[str, list[str]] = {k: [] for k in _FETCHERS}
    for src in [cfg.get("ats_sources_file"), *(cfg.get("ats_sources_files") or [])]:
        if not src:
            continue
        path = Path(src).expanduser()
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for k in _FETCHERS:
                boards[k].extend(str(x) for x in data.get(k) or [] if isinstance(x, (str, int)))
        else:
            log.warning("ats_sources_file not found: %s", path)
    for k, v in (cfg.get("ats_boards") or {}).items():
        if k in boards:
            boards[k].extend(str(x) for x in v or [])
    return {k: sorted(set(v)) for k, v in boards.items()}


def _title_ok(title: str, keywords: list[str], exclude: list[str]) -> bool:
    t = title.lower()
    if any(x.lower() in t for x in exclude):
        return False
    return not keywords or any(k.lower() in t for k in keywords)


def run_ats_discovery(cfg: dict | None = None, workers: int = 8) -> dict:
    """Fetch every configured board, filter, and store new jobs."""
    if cfg is None:
        cfg = config.load_search_config()
    boards = _load_boards(cfg)
    total_boards = sum(len(v) for v in boards.values())
    if not total_boards:
        log.info("No ATS boards configured (ats_boards / ats_sources_file in searches.yaml).")
        return {"new": 0, "existing": 0, "boards": 0, "errors": 0}

    keywords = cfg.get("title_keywords") or [q["query"] for q in cfg.get("queries", []) if q.get("query")]
    exclude = cfg.get("exclude_titles") or []
    max_exp = cfg.get("max_experience_years")
    accept, reject = config.location_filters(cfg)

    init_db()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    new = existing = errors = filtered = 0

    jobs_by_board: list[tuple[str, list[dict]]] = []
    with httpx.Client(timeout=30, headers=_UA, follow_redirects=True) as client, \
            ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_FETCHERS[ats], client, board): (ats, board)
            for ats, names in boards.items() for board in names
        }
        for fut in as_completed(futures):
            ats, board = futures[fut]
            try:
                jobs_by_board.append((ats, fut.result()))
            except Exception as e:
                errors += 1
                log.warning("%s/%s: %s", ats, board, str(e)[:120])

    for ats, jobs in jobs_by_board:
        for j in jobs:
            if not j["url"] or not j["title"]:
                continue
            if not _title_ok(j["title"], keywords, exclude) or \
                    not config.location_ok(j["location"], accept, reject):
                filtered += 1
                continue
            if max_exp is not None and (_min_experience(j) or 0) > max_exp:
                filtered += 1
                continue
            desc = j.get("description") or None
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at, company) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (j["url"], j["title"], j.get("salary"), (desc or "")[:500] or None, j["location"],
                 ats, "ats_api", now, desc, j.get("application_url"),
                 now if desc else None, j["company"]),
            )
            if cur.rowcount:
                new += 1
            else:
                existing += 1
    conn.commit()

    log.info("ATS boards: %d boards, %d new, %d known, %d filtered, %d board errors",
             total_boards, new, existing, filtered, errors)
    return {"new": new, "existing": existing, "filtered": filtered, "boards": total_boards, "errors": errors}
