# Portfolio.AI — portfolio review demo

A responsive Flask + SQLite portfolio review app with dark/light themes, animated UI, accessible form feedback, source-by-source analysis, and evidence-based reports.

## Run locally

```bash
cd portfolio-ai
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

## What the backend evaluates

`POST /api/evaluate` accepts JSON or multipart form data. Any one or more of these fields may be sent:

- `github` — username or GitHub profile URL. Reads public profile/repository metadata and samples README signals from up to five public repositories.
- `resume` — pasted resume text; or `resume_file` — selectable-text PDF up to 5 MB, extracted in memory with pypdf.
- `linkedin` — validates a LinkedIn profile URL only. LinkedIn profile pages are not scraped.
- `linkedin_summary` — optional pasted headline/About/experience/skills text for an actual LinkedIn content review. The pasted text is not stored.
- `portfolio` — public website URL. The backend checks bounded public HTML for title/description, headings, visible-text volume, contact/project cues, HTTPS, and viewport metadata.

A LinkedIn URL by itself is reported as a format check, **not assigned a fake content score**. For a content review, paste the profile text. A portfolio page that is blocked, private, redirects, or returns non-HTML gets a visible unavailable status and no score. Its HTML is limited to 500 KB; fetches time out quickly, private/local network destinations and nonstandard ports are rejected, and redirects are blocked. The check does not execute JavaScript, inspect screenshots, or measure performance.

Resume PDFs must contain selectable text; scanned image-only PDFs are not OCR'd. The original PDF, extracted resume text, and pasted LinkedIn text are never saved. Only derived report fields are retained for scored reports.

## API routes

- `GET /api/health` — service health check.
- `POST /api/evaluate` — generate a report from one or more inputs.
- `GET /api/reports` — latest 20 stored scored reports (metadata only).
- `GET /api/reports/<id>` — retrieve one stored scored report.

When every provided source is unavailable or only a URL-format check is possible, the API returns an informative report with `score: null`; it does not invent an overall score or store that report.

Example:

```bash
curl -X POST http://127.0.0.1:5000/api/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"github":"octocat","resume":"Software engineer. Projects: built a React app used by 200 users. Skills: JavaScript, SQL. Education: computer science degree."}'
```

PDF example:

```bash
curl -X POST http://127.0.0.1:5000/api/evaluate \
  -F 'name=Resume review' -F 'resume_file=@resume.pdf;type=application/pdf'
```

Optional LinkedIn text example:

```bash
curl -X POST http://127.0.0.1:5000/api/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"linkedin":"linkedin.com/in/your-name","linkedin_summary":"Software engineer. About: ... Experience: ... Skills: Python, SQL."}'
```

## GitHub rate limits

GitHub's public API is available without a token, but anonymous requests have a lower rate limit. To raise it for a local deployment, set `GITHUB_TOKEN` in the server environment before starting Flask. Keep it out of source files and logs. The app never returns or stores the token.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers individual and combined signals, PDF privacy, GitHub fallbacks, LinkedIn text vs URL-only behavior, portfolio HTML analysis, private-network rejection, and no-score reporting.

## Product and deployment notes

- Scores are explainable rules-based evidence checks, not hiring predictions, professional advice, skill-proficiency ratings, or a generative AI judgment.
- The public portfolio check is intentionally lightweight and cannot judge visual design or JavaScript-rendered content.
- Job-match examples, sample landing-page scores/statistics, and testimonials are illustrative UI content, not backend-calculated results or verified claims.
- Reports are stored in local SQLite without authentication. This is a demo; add authentication, access controls, retention rules, and production hardening before exposing it publicly or processing sensitive user data.
- The interface uses a rich oxblood, rose, and antique-gold palette with a remembered dark/light mode and larger, high-contrast typography.
