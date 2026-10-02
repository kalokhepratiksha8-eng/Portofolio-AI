from __future__ import annotations

import base64
import json
import os
import ipaddress
import socket
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import unescape
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PORTFOLIO_AI_DB", BASE_DIR / "reports.db"))
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 40_000
MAX_LINKEDIN_CHARS = 12_000
app = Flask(__name__)
# Leave headroom for multipart boundaries and the other optional text fields.
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

SKILL_TERMS = {
    "Python": ("python",), "JavaScript": ("javascript", "js"), "TypeScript": ("typescript",),
    "React": ("react",), "Flask": ("flask",), "Django": ("django",), "FastAPI": ("fastapi",),
    "Node.js": ("node.js", "nodejs"), "SQL": ("sql", "postgresql", "mysql"),
    "Docker": ("docker",), "AWS": ("aws", "amazon web services"), "Git": ("git", "github"),
    "HTML/CSS": ("html", "css"), "REST APIs": ("rest api", "restful", "api development"),
    "Java": ("java",), "C++": ("c++",), "C#": ("c#",), "Go": ("golang",),
    "Kubernetes": ("kubernetes", "k8s"), "Machine Learning": ("machine learning", " scikit-learn", "pytorch", "tensorflow"),
    "Testing": ("pytest", "unit test", "testing", "jest"),
}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, display_name TEXT NOT NULL,
            total_score INTEGER NOT NULL, report_json TEXT NOT NULL
        )""")


def clean_text(value, limit=MAX_RESUME_CHARS):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean_url(value):
    value = clean_text(value, 500)
    if not value:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) and not re.match(r"^https?://", value, re.I):
        raise ValueError("Please enter a valid http or https URL.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Please enter a valid http or https URL.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or " " in parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Please enter a valid http or https URL.")
    return value


def github_username(value):
    value = clean_text(value, 300)
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}", value):
        return value
    parsed = urlparse(value if re.match(r"^https?://", value, re.I) else "https://" + value)
    if parsed.hostname and parsed.hostname.lower() in ("github.com", "www.github.com"):
        pieces = [part for part in parsed.path.split("/") if part]
        if len(pieces) == 1 and re.fullmatch(r"[A-Za-z0-9-]{1,39}", pieces[0]):
            return pieces[0]
    raise ValueError("Enter a GitHub username or a github.com profile URL.")


def github_request(url, accept="application/vnd.github+json", limit=1_000_000):
    headers = {
        "User-Agent": "PortfolioAI-Demo/2.1",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read(limit)


def readme_evidence(owner, repo):
    """Fetch only a small public README response; never persist the file contents."""
    repo_name = urllib.parse.quote(str(repo.get("name", "")), safe="")
    if not repo_name:
        return {"available": False, "quality_score": 0, "word_count": 0, "sections": 0}
    url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{repo_name}/readme"
    try:
        raw = github_request(url, "application/vnd.github.raw+json", 240_000)
        text = ""
        if raw.lstrip().startswith(b"{"):
            obj = json.loads(raw)
            encoded = obj.get("content", "")
            text = base64.b64decode(encoded).decode("utf-8", "replace") if encoded else ""
        else:
            text = raw.decode("utf-8", "replace")
        text = text[:200_000]
        words = re.findall(r"\b[\w+#.-]+\b", text)
        headings = len(re.findall(r"(?m)^#{1,3}\s+", text))
        quality = min(100, (25 if len(words) >= 40 else 10 if words else 0) + min(headings, 5) * 10 + (15 if re.search(r"(?i)install|usage|setup|getting started", text) else 0) + (10 if re.search(r"(?i)license|contributing", text) else 0))
        return {"available": bool(text), "quality_score": quality, "word_count": len(words), "sections": headings}
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {"available": False, "quality_score": 0, "word_count": 0, "sections": 0}


def github_data(value):
    username = github_username(value)
    try:
        profile = json.loads(github_request(f"https://api.github.com/users/{urllib.parse.quote(username)}", limit=256_000))
        repos_data = json.loads(github_request(
            f"https://api.github.com/users/{urllib.parse.quote(username)}/repos?per_page=100&sort=updated", limit=1_000_000))
        if not isinstance(repos_data, list):
            repos_data = []
        repos = [repo for repo in repos_data if isinstance(repo, dict) and not repo.get("fork")]
        readmes = {}
        # Inspect README evidence for a small sample to keep API use and latency bounded.
        for repo in repos[:5]:
            readmes[repo.get("name", "")] = readme_evidence(username, repo)
        return {"username": username, "profile": profile, "repos": repos, "readmes": readmes}, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, f"Unable to access this source. GitHub user “{username}” was not found. Evaluation was based on the other available signals."
        return None, "Unable to access this source. GitHub could not be reached; evaluation was based on the other available signals."
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None, "Unable to access this source. GitHub could not be reached; evaluation was based on the other available signals."


def recent_days(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)
    except (TypeError, ValueError):
        return None


def score_github(data):
    profile, repos, readmes = data["profile"], data["repos"], data["readmes"]
    languages = sorted({r.get("language") for r in repos if r.get("language")})
    described = sum(bool(r.get("description")) for r in repos)
    topic_repos = sum(bool(r.get("topics")) for r in repos)
    recent = sum(1 for r in repos if (days := recent_days(r.get("pushed_at"))) is not None and days <= 180)
    documented = sum(bool(readmes.get(r.get("name", ""), {}).get("available")) for r in repos[:5])
    score = 30 + min(len(repos), 10) * 3
    if profile.get("bio"): score += 8
    if profile.get("blog"): score += 4
    if repos and described / len(repos) >= .5: score += 8
    if repos and topic_repos: score += 6
    if repos and documented: score += 10
    if recent: score += 8
    if languages: score += min(8, len(languages) * 2)
    if any(r.get("stargazers_count", 0) for r in repos): score += 5

    projects = []
    for repo in repos[:5]:
        readme = readmes.get(repo.get("name", ""), {})
        size = max(0, int(repo.get("size") or 0))
        projects.append({
            "name": repo.get("name", "Repository"),
            "technology": repo.get("language") or "Not specified in GitHub metadata",
            "description": clean_text(repo.get("description") or "No public repository description.", 240),
            "stars": int(repo.get("stargazers_count") or 0),
            "repository_size_kb": size,
            "complexity_proxy": "Larger repository footprint" if size >= 10_000 else "Moderate repository footprint" if size >= 1_000 else "Small repository footprint",
            "documentation": "README found" if readme.get("available") else "README not found or could not be checked",
            "readme_quality_score": readme.get("quality_score", 0),
            "readme_word_count": readme.get("word_count", 0),
            "recent_push_days": recent_days(repo.get("pushed_at")),
            "homepage_linked": bool(repo.get("homepage")),
        })
    tips = []
    if not profile.get("bio"):
        tips.append({"text": "Add a concise GitHub bio describing what you build and the roles you are targeting.", "basis": "No public bio was returned by GitHub."})
    if not repos:
        tips.append({"text": "Add a few public projects that demonstrate your current skills.", "basis": "No non-fork public repositories were returned."})
    elif described < len(repos):
        tips.append({"text": "Add concise descriptions to public repositories that are missing them.", "basis": f"{len(repos) - described} of {len(repos)} non-fork repositories had no description."})
    if repos and not documented:
        tips.append({"text": "Add a README to a representative repository with setup, usage, and project context.", "basis": "No README was detected in the checked repositories."})
    if repos and not recent:
        tips.append({"text": "If these projects are still active, push a small update and refresh the project documentation.", "basis": "No checked repository showed a push in the last 180 days."})
    if not profile.get("blog"):
        tips.append({"text": "Link your portfolio in the GitHub profile website field if you have one.", "basis": "The public profile had no website URL."})
    if not tips:
        tips.append({"text": "Keep the strongest projects easy to scan with clear documentation and recent, relevant examples.", "basis": "General maintenance suggestion based on the available repository metadata."})

    return {
        "score": min(100, score),
        "summary": f"{profile.get('public_repos', len(repos))} public repos · {len(languages)} detected languages · {documented}/{min(5, len(repos))} checked READMEs found",
        "analysis_scope": "Public profile metadata, non-fork repositories, repository languages, descriptions, topics, stars, push dates, and README presence/structure for up to five repositories.",
        "signals": [x for x in ["Profile bio" if profile.get("bio") else None, "Website linked" if profile.get("blog") else None, "Repository descriptions" if described else None, "README evidence" if documented else None, "Recent pushes" if recent else None] if x],
        "languages": languages[:12], "projects": projects, "tips": tips[:5],
    }


def extract_resume_pdf(file_storage):
    filename = clean_text(file_storage.filename, 180)
    if not filename or not filename.lower().endswith(".pdf"):
        raise ValueError("Please choose a PDF file for the resume.")
    data = file_storage.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("Resume PDF must be 5 MB or smaller.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("This file does not look like a valid PDF. Please choose a readable PDF.")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise ValueError("This PDF is password-protected. Please upload an unlocked PDF.")
            except Exception as exc:
                if isinstance(exc, ValueError):
                    raise
                raise ValueError("This PDF is password-protected or cannot be opened. Please upload an unlocked PDF.") from exc
        extracted = []
        for page in reader.pages[:50]:
            text = page.extract_text() or ""
            if text:
                extracted.append(text)
            if sum(len(x) for x in extracted) >= MAX_RESUME_CHARS:
                break
        text = clean_text("\n".join(extracted), MAX_RESUME_CHARS)
        if not text:
            raise ValueError("No selectable text was found in this PDF. Try a text-based PDF or paste the resume text instead.")
        return text, filename, len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("We could not read this PDF. It may be corrupted or scanned as an image; try another PDF or paste the text.") from exc


def score_resume(text, origin="pasted text"):
    text = clean_text(text, MAX_RESUME_CHARS)
    words = re.findall(r"\b[\w+#.-]+\b", text)
    low = " " + text.lower() + " "
    categories = {
        "Experience": ("experience", "work history", "internship", "employment", "worked at", "professional experience"),
        "Projects": ("project", "built", "developed", "created", "implemented"),
        "Skills": ("skills", "technologies", "python", "javascript", "react", "sql"),
        "Education": ("education", "university", "degree", "bachelor", "master", "college"),
        "Impact": ("%", "increased", "reduced", "improved", "saved", "users", "revenue", "decreased"),
    }
    present = [name for name, terms in categories.items() if any(term in low for term in terms)]
    detected_skills = [name for name, terms in SKILL_TERMS.items() if any(re.search(r"(?<![\w+#])" + re.escape(term.strip()) + r"(?![\w+#])", low, re.I) for term in terms)]
    has_numbers = bool(re.search(r"\b\d+\s*%|\b\d+[+,]?\s*(users|clients|projects|hours)\b", low))
    score = min(35, len(words) // 7) + min(40, len(present) * 8) + (12 if len(detected_skills) >= 3 else min(12, len(detected_skills) * 4)) + (13 if has_numbers else 0)
    tips = []
    if "Experience" not in present:
        tips.append({"text": "Add an experience section, including internships, volunteering, or relevant coursework.", "basis": "No common experience-section signals were found in the provided text."})
    if "Projects" not in present:
        tips.append({"text": "Describe 2–3 projects, your specific contribution, and the technologies used.", "basis": "No common project/action signals were found in the provided text."})
    if "Education" not in present:
        tips.append({"text": "Add an education section with your degree, institution, or relevant training.", "basis": "No common education-section signals were found in the provided text."})
    if not has_numbers:
        tips.append({"text": "Where accurate, add measurable outcomes to experience or project bullets.", "basis": "No numeric impact evidence was detected in the provided text."})
    if not detected_skills:
        tips.append({"text": "Name relevant tools and technical skills explicitly in a dedicated skills section.", "basis": "No supported skill names were detected in the provided text."})
    if not tips:
        tips.append({"text": "Tailor the summary and strongest bullets to each role, keeping each achievement specific.", "basis": "General editing suggestion after common sections and impact signals were present."})
    skill_gaps = [skill for skill in ("TypeScript", "Docker", "AWS", "Testing", "REST APIs", "SQL") if skill not in detected_skills]
    return {
        "score": min(100, score),
        "summary": f"{len(words)} words · {len(present)}/5 content sections · {len(detected_skills)} supported skills named · {origin}",
        "analysis_scope": "Text structure, common experience/projects/skills/education/impact signals, and explicit mentions of a curated set of technical skills. This does not establish actual ability or guarantee ATS performance.",
        "signals": present, "skills": detected_skills, "skill_gaps": skill_gaps[:6], "tips": tips[:5],
    }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_http_target(url):
    """Reject private, local, nonstandard-port, and otherwise unsafe URL targets."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme not in ("http", "https") or not host or port not in (80, 443):
            return False
        if parsed.username or parsed.password or host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".test")):
            return False
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = {ipaddress.ip_address(row[4][0]) for row in addresses}
        return bool(ips) and all(ip.is_global for ip in ips)
    except (ValueError, OSError, socket.gaierror):
        return False


def fetch_public_page(url):
    """Read bounded public HTML only; reject private targets and all redirects."""
    if not _public_http_target(url):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": "PortfolioAI-Review/2.1 (+public portfolio metadata check)",
        "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(req, timeout=5) as response:
            raw = response.read(500_001)
            content_type = (response.headers.get("Content-Type") or "").lower()
            if len(raw) > 500_000 or ("html" not in content_type and b"<html" not in raw[:2000].lower()):
                return None
            charset = response.headers.get_content_charset() or "utf-8"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    text = raw.decode(charset, "replace")
    def tag(pattern):
        match = re.search(pattern, text, re.I | re.S)
        return re.sub(r"\s+", " ", unescape(match.group(1))).strip()[:240] if match else ""
    title = tag(r"<title[^>]*>(.*?)</title>")
    description = (tag(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']')
                   or tag(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']'))
    headings = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", text, re.I | re.S)
    headings = [re.sub(r"<[^>]+>", " ", unescape(re.sub(r"\s+", " ", h))).strip()[:120] for h in headings]
    visible = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>|<[^>]+>", " ", text, flags=re.I | re.S)
    visible = re.sub(r"\s+", " ", unescape(visible))
    words = len(re.findall(r"\b[\w+#.-]{2,}\b", visible))
    contact = bool(re.search(r"mailto:|\bcontact\b|\bemail\b|[\w.+-]+@[\w-]+\.[\w.-]+", text, re.I))
    projects = bool(re.search(r"\bprojects?\b|case studies|\bportfolio\b|\blive demo\b|github\.com", visible, re.I))
    viewport = bool(re.search(r'<meta[^>]+name=["\']viewport["\']', text, re.I))
    return {"title": title, "description": description, "headings": [h for h in headings if h][:8],
            "word_count": words, "contact_cue": contact, "project_cue": projects, "responsive_viewport": viewport}


def check_linkedin_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = [part for part in parsed.path.split("/") if part]
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and len(path) >= 2 and path[0].lower() == "in"


def score_linkedin_summary(text, has_profile_url=False):
    text = clean_text(text, MAX_LINKEDIN_CHARS)
    low = text.lower()
    words = len(text.split())
    skills = detected_resume_skills(text)
    checks = [
        ("Clear target role", 10, bool(re.search(r"\b(engineer|developer|designer|analyst|manager|intern|specialist|consultant|researcher|student)\b", low))),
        ("Substantial About / profile summary", 10, words >= 45),
        ("Experience evidence", 10, bool(re.search(r"\b(experience|work history|employment|internship|intern|role)\b", low))),
        ("Projects or featured work", 10, bool(re.search(r"\b(project|portfolio|featured|case study|github|demo)\b", low))),
        ("Measurable outcomes", 15, bool(re.search(r"\d+(?:\.\d+)?\s?%|\b\d+\s?(?:users|customers|clients|projects|requests|students|members)\b|\b\d+x\b", text, re.I))),
        ("Action-oriented language", 15, len(re.findall(r"\b(built|developed|designed|led|launched|improved|reduced|increased|delivered|managed|created|implemented|optimized|automated)\b", low)) >= 3),
        ("Profile URL supplied", 5, has_profile_url),
    ]
    earned = sum(points for _, points, passed in checks if passed) + min(20, len(skills) * 4)
    tips = [{"text": f"Strengthen the {label.lower()} signal in your profile.", "basis": f"Not detected in the {words}-word text you supplied."}
            for label, _, passed in checks if not passed]
    if len(skills) < 4:
        tips.append({"text": "Name relevant tools and skills explicitly in the headline, About, or experience sections.", "basis": f"{len(skills)} supported skill keyword(s) detected in the supplied text."})
    return {"score": min(100, earned), "summary": f"{words} words reviewed · {sum(1 for _, _, passed in checks if passed)} of {len(checks)} profile signals found · {len(skills)} supported skills named",
            "analysis_scope": "Rules-based review of only the LinkedIn headline/About/experience text you pasted. LinkedIn pages are not scraped; this is not a proficiency or hiring score.",
            "signals": [label for label, _, passed in checks if passed], "skills": skills, "tips": tips[:6]}


def score_portfolio_page(url):
    page = fetch_public_page(url)
    if not page:
        return None, "Portfolio: the URL was accepted, but no readable public HTML was returned. Private, blocked, redirected, or JavaScript-only pages cannot be evaluated."
    checks = [
        ("HTTPS", 8, urlparse(url).scheme == "https"),
        ("Descriptive page title", 12, len(page["title"]) >= 8),
        ("Meta description", 10, len(page["description"]) >= 30),
        ("Main heading", 10, bool(page["headings"])),
        ("Section structure", 10, len(page["headings"]) >= 3),
        ("Readable page text", 15, page["word_count"] >= 100),
        ("Contact cue", 10, page["contact_cue"]),
        ("Project / case-study cue", 15, page["project_cue"]),
        ("Responsive viewport", 10, page["responsive_viewport"]),
    ]
    score = sum(points for _, points, found in checks if found)
    tips = [{"text": f"Add or improve the {label.lower()}.", "basis": f"Not detected in the public HTML fetched from {urlparse(url).hostname}."}
            for label, _, found in checks if not found]
    if not tips:
        tips.append({"text": "Keep project case studies current and include your role, stack, outcomes, and live/source links.", "basis": "General next step; the automated check does not judge visual quality or project quality."})
    return {"score": score, "summary": f"Public portfolio HTML reviewed · {page['word_count']} words · {len(page['headings'])} headings",
            "analysis_scope": "A bounded public HTML check (max 500 KB, no redirects or JavaScript) for title, description, headings, visible-text volume, contact/project cues, HTTPS, and viewport metadata. Not a visual, performance, or code audit.",
            "signals": [label for label, _, found in checks if found], "page_metadata": {k: page[k] for k in ("title", "description", "headings", "word_count")}, "tips": tips[:8]}, None


def detected_resume_skills(text):
    low = " " + clean_text(text, MAX_RESUME_CHARS).lower() + " "
    return [name for name, terms in SKILL_TERMS.items() if any(re.search(r"(?<![\w+#])" + re.escape(term.strip()) + r"(?![\w+#])", low, re.I) for term in terms)]


def build_readiness(modules):
    result = []
    if "github" in modules:
        result.extend([
            {"category": "GitHub profile", "score": modules["github"]["score"], "basis": "Public profile and repository metadata."},
            {"category": "Projects", "score": modules["github"]["score"], "basis": "Repository descriptions, README evidence, topics, stars, and activity dates."},
        ])
        if modules["github"].get("languages"):
            result.append({"category": "Technical skills", "score": min(100, 45 + len(modules["github"]["languages"]) * 8), "basis": "Languages explicitly listed in public repositories; not a proficiency measure."})
    if "resume" in modules:
        result.extend([
            {"category": "Resume structure", "score": modules["resume"]["score"], "basis": "Content signals found in supplied resume text."},
            {"category": "Technical skills", "score": min(100, 40 + len(modules["resume"].get("skills", [])) * 10), "basis": "Supported skill names present in supplied text; not a proficiency measure."},
        ])
    if "linkedin" in modules:
        result.append({"category": "LinkedIn profile text", "score": modules["linkedin"]["score"], "basis": "Rules-based checks on the user's pasted profile text; the page itself is not scraped."})
    if "portfolio" in modules:
        result.append({"category": "Portfolio HTML signals", "score": modules["portfolio"]["score"], "basis": "Bounded public HTML structure checks; visual quality and JavaScript rendering are not evaluated."})
    return result


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "Portfolio.AI API"})


@app.post("/api/evaluate")
def evaluate():
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload = payload or {}
    warnings = []
    resume_file = request.files.get("resume_file")
    resume_from_file = resume_filename = ""
    resume_pages = None
    if resume_file and resume_file.filename:
        try:
            resume_from_file, resume_filename, resume_pages = extract_resume_pdf(resume_file)
        except ValueError as exc:
            warnings.append(str(exc))
    resume_text = clean_text(payload.get("resume"), MAX_RESUME_CHARS)
    resume = clean_text("\n".join(x for x in (resume_text, resume_from_file) if x), MAX_RESUME_CHARS)
    try:
        github_input = clean_text(payload.get("github"), 300)
        linkedin_raw = clean_text(payload.get("linkedin"), 500)
        linkedin_summary = clean_text(payload.get("linkedin_summary"), MAX_LINKEDIN_CHARS)
        portfolio_raw = clean_text(payload.get("portfolio"), 500)
        linkedin = portfolio = ""
        if linkedin_raw:
            try:
                linkedin = clean_url(linkedin_raw)
            except ValueError as exc:
                warnings.append(f"LinkedIn URL was invalid ({exc}).")
        if portfolio_raw:
            try:
                portfolio = clean_url(portfolio_raw)
            except ValueError as exc:
                warnings.append(f"Portfolio URL was invalid ({exc}).")
        name = clean_text(payload.get("name"), 80) or "Your report"
        if not any([github_input, resume, linkedin_raw, linkedin_summary, portfolio_raw, resume_file and resume_file.filename]):
            return jsonify({"error": "Please provide at least one signal to generate your review."}), 400

        modules = {}
        source_status = {key: {"status": "not_provided", "label": "Not provided"}
                         for key in ("github", "resume", "linkedin", "portfolio")}
        extra_tips = []
        if resume_file and resume_file.filename and not resume:
            source_status["resume"] = {"status": "unavailable", "label": "PDF could not be read"}

        if github_input:
            try:
                gh_data, warning = github_data(github_input)
            except ValueError as exc:
                gh_data, warning = None, f"GitHub input was invalid ({exc})."
            if warning:
                warnings.append(warning)
                source_status["github"] = {"status": "unavailable", "label": "Unable to access public GitHub data"}
            elif gh_data:
                modules["github"] = score_github(gh_data)
                source_status["github"] = {"status": "analyzed", "label": "Public GitHub profile and repositories analyzed"}

        if resume:
            origin = "PDF text extracted in memory" if resume_from_file else "pasted text"
            modules["resume"] = score_resume(resume, origin)
            if resume_filename:
                modules["resume"]["filename"] = resume_filename
                modules["resume"]["pdf_pages"] = resume_pages
            source_status["resume"] = {"status": "analyzed", "label": "Resume text analyzed", "filename": resume_filename or None}

        linkedin_valid = bool(linkedin and check_linkedin_url(linkedin))
        if linkedin_raw and not linkedin:
            source_status["linkedin"] = {"status": "unavailable", "label": "Invalid URL"}
        elif linkedin and not linkedin_valid:
            source_status["linkedin"] = {"status": "unavailable", "label": "Expected a linkedin.com/in/profile URL"}
            warnings.append("LinkedIn: that is not a valid linkedin.com/in profile URL.")
        elif linkedin_valid:
            source_status["linkedin"] = {"status": "link_checked", "label": "Profile URL format checked · page not scraped"}
        if linkedin_summary:
            modules["linkedin"] = score_linkedin_summary(linkedin_summary, has_profile_url=linkedin_valid)
            source_status["linkedin"] = {"status": "analyzed", "label": "Pasted profile text analyzed" + (" · URL checked" if linkedin_valid else "")}
        elif linkedin_valid:
            extra_tips.append({"module": "linkedin", "text": "For an actual LinkedIn content review, paste your headline, About, experience, and skills text into the optional profile-text field.", "basis": "LinkedIn page content is not scraped; only the profile URL format was checked."})

        if portfolio_raw and not portfolio:
            source_status["portfolio"] = {"status": "unavailable", "label": "Invalid URL"}
        elif portfolio:
            portfolio_module, warning = score_portfolio_page(portfolio)
            if warning:
                warnings.append(warning)
                source_status["portfolio"] = {"status": "unavailable", "label": "Public page could not be read"}
            elif portfolio_module:
                modules["portfolio"] = portfolio_module
                source_status["portfolio"] = {"status": "analyzed", "label": "Public page structure analyzed"}

        if not modules and not extra_tips:
            extra_tips.append({"module": "report", "text": "No source returned scorable content, so no overall score was generated.", "basis": "Add resume text/PDF, a public GitHub profile, a reachable public portfolio, or pasted LinkedIn profile text."})

        resume_skills = modules.get("resume", {}).get("skills", [])
        github_languages = modules.get("github", {}).get("languages", [])
        linkedin_skills = modules.get("linkedin", {}).get("skills", [])
        evidence_sets = {"Resume text": resume_skills, "GitHub repository metadata": github_languages, "Pasted LinkedIn text": linkedin_skills}
        skill_evidence = []
        for skill in sorted(set(resume_skills + github_languages + linkedin_skills), key=str.lower):
            sources = [label for label, skills in evidence_sets.items() if skill in skills]
            skill_evidence.append({"name": skill, "sources": sources, "evidence_count": len(sources)})
        skill_gaps = []
        if "resume" in modules:
            skill_gaps = [{"name": skill, "basis": "Not named in the submitted resume text; this does not establish that you lack the skill."}
                          for skill in modules["resume"].get("skill_gaps", [])]

        cross_signal = None
        if sum(bool(skills) for skills in evidence_sets.values()) >= 2:
            shared = sorted([item["name"] for item in skill_evidence if item["evidence_count"] > 1], key=str.lower)
            cross_signal = {"shared_skills": shared,
                            "summary": f"{len(shared)} skill name(s) appear in more than one supplied or retrieved source." if shared else "No exact supported skill names were matched across the available sources.",
                            "basis": "Exact keyword overlap only; not a measure of proficiency or project quality."}

        total = round(sum(item["score"] for item in modules.values()) / len(modules)) if modules else None
        tips = []
        for key, module in modules.items():
            for tip in module.get("tips", []):
                tips.append({"module": key, "text": tip["text"], "basis": tip.get("basis", module.get("analysis_scope", ""))})
        tips.extend(extra_tips)
        report = {
            "id": uuid.uuid4().hex[:12], "created_at": datetime.now(timezone.utc).isoformat(),
            "name": name, "score": total, "modules": modules, "source_status": source_status,
            "provided_signals": [key for key, status in source_status.items() if status["status"] != "not_provided"],
            "skills": skill_evidence, "skill_gaps": skill_gaps,
            "projects": modules.get("github", {}).get("projects", []),
            "cross_signal": cross_signal, "readiness": build_readiness(modules),
            "tips": tips[:12], "warnings": warnings,
            "disclaimer": "Scores are transparent rules-based evidence checks, not hiring predictions or proficiency measures. LinkedIn pages are not scraped; pasted profile text is analyzed. Portfolio checks fetch only bounded public HTML with redirects blocked. Resume PDF bytes and raw resume/profile text are not stored.",
            "stored": bool(total is not None),
        }
        # Persist only scored summaries, never raw resume bytes/text or pasted LinkedIn profile text.
        if total is not None:
            with db_connect() as conn:
                conn.execute("INSERT INTO reports VALUES (?, ?, ?, ?, ?)",
                             (report["id"], report["created_at"], name, total, json.dumps(report)))
        return jsonify(report)
    except ValueError as exc:
        return jsonify({"error": str(exc), "warnings": warnings}), 400


@app.get("/api/reports")
def reports():
    with db_connect() as conn:
        rows = conn.execute("SELECT id, created_at, display_name, total_score FROM reports ORDER BY created_at DESC LIMIT 20").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/reports/<report_id>")
def report_detail(report_id):
    with db_connect() as conn:
        row = conn.execute("SELECT report_json FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return jsonify({"error": "Report not found."}), 404
    return jsonify(json.loads(row["report_json"]))


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "The upload is too large. Resume PDFs must be 5 MB or smaller."}), 413


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
