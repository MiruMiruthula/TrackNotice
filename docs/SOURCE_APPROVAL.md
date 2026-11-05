# Source Approval Document  (Student task 01 / Mentor checkpoint CP-1)

Fill this in **before** enabling any real source in `sources.json`. Keep a dated copy in Git.

| Item | Entry |
|---|---|
| Student / team | |
| Agency & official page URL | |
| Date reviewed | |
| Page type | HTML table / HTML cards / RSS / Atom / JSON API / GTFS-Realtime alerts |
| Is a **feed or API** available? (use it instead of scraping) | yes / no - URL: |
| `robots.txt` URL and result for the advisory path | allowed / disallowed (paste the relevant lines) |
| Terms of use / licence summary | automated access permitted? attribution needed? |
| Login, CAPTCHA, paywall or personal data on the page? | must be **no** - otherwise STOP and choose another source |
| Rate limit we will use | e.g. 1 request / 15 min, `min_delay_seconds: 10` |
| Identification | User-Agent contains project name + contact e-mail |
| Fields available -> our fields | route, area, type, summary, published, valid_from, valid_to, link |
| Time zone & date format used by the page | |
| Refresh schedule | |
| Failure handling | timeout 10 s, 3 attempts, exponential backoff, error shown on UI |
| Risks | layout change, rate limiting, missing end dates, notices published only as images/PDF |
| Alternative if permission is refused | official RSS / e-mail subscription / open-data portal / manual entry demo |
| Mentor approval | name / date / signature |

## Candidate sources to evaluate (verify each yourself - do not assume permission)
* A state transport corporation's "notices / circulars / route changes" page.
* A metro-rail operator's "service updates / press releases" page.
* A city open-data portal or a GTFS-Realtime *Service Alerts* feed (preferred: structured and intended for reuse).

If robots.txt or the terms forbid automated access, **do not scrape** - use the built-in demo source and document why.
