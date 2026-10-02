# Portfolio.AI — implementation notes

## Frontend

- Preserves the supplied Portfolio.AI page structure and animated landing-page sections.
- Uses an oxblood/rose/antique-gold visual system with a persistent, labeled dark/light toggle.
- Increases the base type scale, raises undersized labels to a readable minimum, and strengthens contrast in light mode.
- Respects `prefers-reduced-motion`, includes responsive navigation, scroll reveals, focus states, and accessible modal/form feedback.
- The review dialog supports one or more inputs, PDF drag/drop and pasted resume text, LinkedIn profile text, live source status, upload progress, and an evidence-aware animated report.
- Reports with only a URL-format check or no readable source show “no score” rather than inventing a score.

## Backend analysis

- `POST /api/evaluate` accepts JSON and multipart form data: `github`, `resume`, `resume_file`, `linkedin`, `linkedin_summary`, and `portfolio`.
- **GitHub:** calls the public REST API for profile/repository metadata and up to five README samples. An optional `GITHUB_TOKEN` environment variable can be used for a higher API quota; secrets are not returned or persisted.
- **Resume:** checks common structure, impact language, and supported skill mentions. Text PDFs up to 5 MB are extracted with pypdf in memory; scanned image PDFs are not OCR'd.
- **LinkedIn:** validates the `linkedin.com/in/...` URL but does not scrape LinkedIn. If the user pastes headline/About/experience/skills text, the backend applies a transparent checklist to that text. Raw pasted text is not saved.
- **Portfolio:** safely requests a small amount of public HTML and evaluates title, description, headings, word count, HTTPS, responsive viewport, and contact/project cues. It does not execute JavaScript or assess appearance/performance. Private/local IPs, nonstandard ports, and redirects are rejected; response size and timeout are bounded.
- Failed or inaccessible sources do not block valid signals. If no source returns scorable data, the API returns a helpful report with `score: null`, no fabricated modules, and does not persist that report.
- Scored report records include derived results, not raw resume or LinkedIn text. No PDF file is written to disk.

## API routes

- `GET /api/health`
- `POST /api/evaluate`
- `GET /api/reports`
- `GET /api/reports/<id>`

## Validation

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile app.py tests/test_app.py
```

Regression tests cover empty-input validation, text/PDF resume processing and privacy, GitHub + resume combinations, LinkedIn URL-only/no-score and pasted-text scoring, safe portfolio-page checks and no-score fallback, malformed/unavailable sources, and private-target rejection. Manual integration checks also exercise a live public GitHub profile and a public HTML page.

## Remaining demo limitations

- LinkedIn's page content cannot be retrieved without an official authorized integration; the app intentionally uses pasted text instead of scraping.
- Portfolio scoring is based on raw HTML heuristics, not a browser render or professional design audit.
- GitHub anonymous rate limits can apply if `GITHUB_TOKEN` is not set.
- Job-match panels and sample statistics elsewhere on the landing page remain illustrative.
- SQLite reports have no authentication; do not expose this demo publicly with sensitive data before adding access controls and retention rules.
