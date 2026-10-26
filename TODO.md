# TODO: merge job-apply-agent + ApplyPilot

Goal: one tool that finds jobs **in India** (startups first) across Gurgaon/Gurugram, Delhi, Noida,
Mumbai, Bangalore, Pune and Hyderabad, applies daily, and emails HR where there is no form.
Never apply where Indian candidates are not accepted.

Status: research in progress (2026-10-27). Merge architecture section gets filled in once the
job-apply-agent map is done.

## 1. Merge (keep the best of both)
- [ ] Decide the base project and folder layout (pending job-apply-agent map)
- [ ] Keep from job-apply-agent: answers.yaml rule engine (regex + india: variants, salary-in-range logic),
      Chrome-extension handoff (forms open in your own Chrome), categories/ranking, verified sources.yaml,
      tracker UI, LLM router (NVIDIA -> Groq ...), never_claim / apply_titles guards
- [ ] Keep from ApplyPilot (refine branch): locked-down Claude agent (browser-only tools), review mode
      with auto-detected confirmation page, India-first queue + work-auth gate, fast ATS discovery,
      per-job score saving, fallback LLM chain
- [ ] One database, one profile/answers file, one CLI

## 2. Discovery: latest openings, startups first
- [ ] Startup registry per city (research: `research_*.json`, verified against live ATS APIs)
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
- [ ] Windows Task Scheduler job (runs locally; apply needs your Chrome and your Submit click):
  - morning: discover (fresh feed + all boards) -> score -> queue
  - then: fill forms one by one in review mode; desktop notification when a form is ready
  - you enter CAPTCHA / security code and press Submit; confirmation is auto-detected
- [ ] Daily summary (new jobs, ready, applied, expired) as a desktop notification or email to yourself
- [ ] Guard: stop the batch before hitting Claude Max usage limits (cap N forms/day, configurable)

## 4. Gmail sender (HR / careers emails)
- [ ] Find addresses only from public sources: the job post itself, the company careers/contact page,
      "email your resume to ..." postings. Never guess addresses (bounces damage your Gmail sender reputation).
- [ ] Reuse job-apply-agent `mailer.py` / `outreach.py` / `email_template.txt` if they fit
- [ ] Gmail API with OAuth (send-only scope `gmail.send`), not the third-party Gmail MCP
- [ ] Personalised email per job: role, why you fit (from resume), CV_VISHWAM_OCTOBER.pdf attached
- [ ] Safety: one email per company per role, max ~20-30/day, spacing between sends,
      review queue before sending (approve all / per email), log every send in the tracker
- [ ] Track replies (read-only Gmail scope) and mark the job "replied"

## 5. Housekeeping
- [ ] Mindtickle x2 and Hevo Data are still `ready`; confirm whether they were submitted
- [ ] PR the generic fixes upstream to Pickle-Pixel/ApplyPilot (optional)
