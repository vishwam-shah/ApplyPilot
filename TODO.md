# TODO: merge job-apply-agent + ApplyPilot

Goal: one tool that finds jobs **in India** (startups first) across Gurgaon/Gurugram, Delhi, Noida,
Mumbai, Bangalore, Pune and Hyderabad, applies daily, and emails HR where there is no form.
Never apply where Indian candidates are not accepted.

Status: research in progress (2026-10-27). Merge architecture section gets filled in once the
job-apply-agent map is done.

## 1. Merge (decided 2026-10-27: job-apply-agent is the base)
job-apply-agent already has the dashboard, tracker, dedupe, legitimacy scoring, answers.yaml rules,
Chrome-extension handoff, outreach/mailer with send interlocks, Task Scheduler installer and
330+ verified boards. ApplyPilot is a CLI; port its strengths in rather than the other way round.

Port from ApplyPilot (refine branch):
- [ ] Claude Code applier (browser-only, strict MCP, dontAsk, uploads confined to worker dir) as the
      fallback for every job job-apply-agent marks `manual` (Keka, Darwinbox, Zoho, Workday, custom sites)
- [ ] Review-mode confirmation detection from page text (Ashby in-page "Application submitted")
- [ ] Work-authorization gate: country-restricted remote rejected, scorer/agent stop on
      "not open to India"; unify the three India detectors (jobspy_in.is_india, crew._INDIA, Answerer.job_in_india)
- [ ] Location matcher with whole-word patterns and home-market ordering

Speed (job-apply-agent fetch ~11 min vs ApplyPilot ~45 s):
- [ ] SmartRecruiters: parallel detail calls (currently sequential per posting)
- [ ] Workday: cap details, parallel tenants; HackerNews: parallel item fetch
- [ ] Ingest: batch fingerprint lookups instead of one SELECT per job
- [ ] Cache resume.yaml in Answerer._latest_role
- [ ] JobSpy sweep: only 24h-old postings, smaller budget

job-apply-agent bugs to fix in the merge:
- [ ] Chrome handoff truncates long answers / cover letters to 300 chars (answers.py:248)
- [ ] answers log keyed by label: duplicate labels overwrite each other
- [ ] per-company cap ignores `ready` attempts (crew._company_count)
- [ ] ingest never updates URL/id of an existing row; gated rows never refresh last_seen_dt
- [ ] rescore_all skips experience / feed_categories gates and drops the experience reason
- [ ] verify_sources.py: rewrites sources.yaml (loses comments), never prunes dead boards
- [ ] requirements.txt missing pypdf; attachment_filename still "CV_VISHWAM_SEP.pdf"
- [ ] dead code: weworkremotely.py unregistered, legacy jobagent/ + scripts/apply_assist.py
- [ ] profile.salary_bands still USD placeholders

## 2. Discovery: latest openings, startups first

Research done 2026-10-27, saved in `research/india_registry.json`: 211 companies across the 7 cities,
sorted by city, then startup -> scaleup -> large. Checked against live job-board APIs: 134 boards are
reachable, and 130 of them have open engineering roles in India (1,557 roles in total, 189 of them at
39 startups).
- Coverage by ATS: Greenhouse 57 (mostly foreign companies' India offices), custom 42 (11 of them
  Workday), Keka 30, Ashby 18 (AI startups), Lever 17, Darwinbox 13, SmartRecruiters 12, Zoho 10,
  Freshteam 9
- Keka is the main ATS for small Indian startups. Public API:
  `https://<slug>.keka.com/careers/api/jobs/default/active`, and `/careers/api/organization/default/careerportalinfo`
  for the company name. A 403 means the board exists but is switched off. Build this connector first.
- Darwinbox (Zepto, Delhivery, MobiKwik, 1mg, CarDekho, Spinny, BharatPe, upGrad, PW, Porter): only
  renders in a browser and shows mostly non-tech roles. Needs a headless-browser pass.
- Pages that only load in a browser: Juspay, Postman, Setu, Whatfix, Chargebee, Atlan, MoEngage, Zepto,
  plus Kula (CleverTap), TurboHire (Purplle, IDfy), SuccessFactors (PayU), ZappyHire (Credgenics).
- Slugs can't be guessed: Innovaccer = workable `innovaccer-analytics`, WebEngage = keka `webklipper`,
  Spinny = `spinzone`, Spyne = `spyneai`. Wrong same-name boards: greenhouse `slice` (Balkans),
  greenhouse `stage`, `shipsy.keka.com`. Dream Sports' Lever board no longer exists. Groww uses the
  EU-hosted Greenhouse (`job-boards.eu.greenhouse.io`), which needs a different API host.
- Workable rate-limits (429) if you check many accounts in a row: add pacing.
- 41 entries have no slug and about 15 are "weak evidence" (portal found, no tech roles visible).
  Re-check these with the browser pass.
- Naukri 24h harvest: 1,278 postings from 467 companies, but only 100 have a direct careers URL, mostly
  large companies. Good as a fresh-postings feed, poor for finding startups. LinkedIn guest search
  returns 429 after a few queries.

- [ ] Load `research/india_registry.json` into the merged sources (startup-first per city)
- [ ] Keka connector (public API above), then Zoho Recruit, Freshteam, Workday (11 tenants in registry)
- [ ] Headless-browser connector for Darwinbox and custom pages
- [ ] Startup-first ranking: funded/YC/recent-raise flag boosts the queue after the India check
- [ ] "Fresh" feed: Naukri + LinkedIn guest listing via JobSpy, `hours_old=24`, all 7 cities
      (both tested 2026-10-27 and returned postings from the last 24 hours)
- [ ] Career-site harvester: collect `job_url_direct` from Naukri/LinkedIn results, detect the ATS
      (Greenhouse/Lever/Ashby/Workday/Keka/Darwinbox/Zoho/Freshteam/custom), auto-add the board,
      then poll it directly every day
- [ ] New connectors (most Indian startups are NOT on Greenhouse/Lever/Ashby, only 20 of 226 probed):
      Keka, Darwinbox, Zoho Recruit, Freshteam, Workable, SmartRecruiters, Recruitee
- [ ] Custom career pages: route to ApplyPilot smart-extract (LLM page extraction)
- [ ] Google: JobSpy's Google Jobs scraper returns nothing (Google changed its page). Use Google
      Programmable Search JSON API instead (free 100 queries/day, needs your key + search engine ID)
      for `"careers" "<city>" software engineer` and site:keka.com / darwinbox.in queries.
      No raw Google scraping (CAPTCHA-blocked, against terms).

## 3. Daily schedule
- [ ] Extend job-apply-agent scripts/install_schedule.ps1 + run_pipeline.py (already: ingest -> purge
      -> outreach build_queue) with the apply step. Runs locally; apply needs your Chrome and Submit click:
  - morning: discover (fresh feed + all boards) -> score -> queue
  - then: fill forms one by one in review mode; desktop notification when a form is ready
  - you enter CAPTCHA / security code and press Submit; confirmation is auto-detected
- [ ] Daily summary (new jobs, ready, applied, expired) as a desktop notification or email to yourself
- [ ] Guard: stop the batch before hitting Claude Max usage limits (cap N forms/day, configurable)

## 4. Gmail sender (HR / careers emails)
- [ ] Find addresses only from public sources: the job post itself, the company careers/contact page,
      "email your resume to ..." postings. Never guess addresses (bounces damage your Gmail sender reputation).
- [ ] Reuse job-apply-agent outreach: inbox_discovery (role inboxes only, MX-checked), outreach build_queue,
      mailer with 6 send interlocks (enabled flag, approved, MX-verified contact, attachment, 25/day,
      60-day company cooldown). Currently outreach_enabled=False.
- [ ] Sending: SMTP + Gmail App Password works today; optionally move to Gmail API OAuth send-only scope
- [ ] Personalised email per job: role, why you fit (from resume), CV_VISHWAM_OCTOBER.pdf attached
- [ ] Safety: one email per company per role, max ~20-30/day, spacing between sends,
      review queue before sending (approve all / per email), log every send in the tracker
- [ ] Track replies (read-only Gmail scope) and mark the job "replied"

## 5. Housekeeping
- [ ] Mindtickle x2 and Hevo Data are still `ready`; confirm whether they were submitted
- [ ] PR the generic fixes upstream to Pickle-Pixel/ApplyPilot (optional)
