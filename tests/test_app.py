import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# Use a throwaway SQLite DB for test runs before importing the Flask module.
TEST_DB_DIR = tempfile.mkdtemp(prefix="portfolio-ai-tests-")
os.environ["PORTFOLIO_AI_DB"] = os.path.join(TEST_DB_DIR, "reports.db")
import app as portfolio_app  # noqa: E402


def text_pdf(text):
    """Return a small text-based PDF fixture without an extra test dependency."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(output)


GOOD_PAGE = {
    "title": "Alex Developer — Portfolio",
    "description": "Selected software projects, case studies, technical skills, and contact information.",
    "headings": ["Software Developer", "Selected Projects", "About Me", "Contact"],
    "word_count": 250,
    "contact_cue": True,
    "project_cue": True,
    "responsive_viewport": True,
}

LINKEDIN_TEXT = (
    "Software Engineer headline. About: I build reliable Python and React products for users. "
    "Experience: developed APIs, launched projects, and improved service reliability by 35 percent. "
    "Featured projects include a Flask portfolio and GitHub repositories. Skills: Python, React, SQL, Docker. "
    "I led teams and delivered tools used by 250 customers. "
) * 2


class EvaluationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portfolio_app.app.config["TESTING"] = True
        cls.client = portfolio_app.app.test_client()

    def test_empty_payload_returns_at_least_one_signal_message(self):
        response = self.client.post("/api/evaluate", json={"name": "Only a label"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Please provide at least one signal to generate your review.")

    def test_resume_and_pasted_linkedin_text_each_generate_a_scored_module(self):
        response = self.client.post("/api/evaluate", json={"resume": "Experience Python projects skills SQL education bachelor impact 25 percent"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["modules"]), ["resume"])
        response = self.client.post("/api/evaluate", json={"linkedin_summary": LINKEDIN_TEXT})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["modules"]), ["linkedin"])
        self.assertEqual(response.json["source_status"]["linkedin"]["status"], "analyzed")
        self.assertGreater(response.json["modules"]["linkedin"]["score"], 0)
        self.assertEqual(response.json["readiness"][0]["category"], "LinkedIn profile text")

    def test_linkedin_url_only_is_a_format_check_not_a_fake_score(self):
        response = self.client.post("/api/evaluate", json={"linkedin": "linkedin.com/in/example-person"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(response.json["score"])
        self.assertEqual(response.json["modules"], {})
        self.assertEqual(response.json["source_status"]["linkedin"]["status"], "link_checked")
        self.assertFalse(response.json["stored"])

    def test_portfolio_public_html_is_scored_from_returned_signals(self):
        with patch.object(portfolio_app, "fetch_public_page", return_value=GOOD_PAGE):
            response = self.client.post("/api/evaluate", json={"portfolio": "https://portfolio.example.dev"})
        self.assertEqual(response.status_code, 200, response.json)
        module = response.json["modules"]["portfolio"]
        self.assertEqual(module["score"], 100)
        self.assertIn("Responsive viewport", module["signals"])
        self.assertIn("page_metadata", module)
        self.assertEqual(response.json["source_status"]["portfolio"]["status"], "analyzed")
        self.assertEqual(response.json["readiness"][0]["category"], "Portfolio HTML signals")

    def test_inaccessible_portfolio_returns_no_score_and_no_persisted_row(self):
        with patch.object(portfolio_app, "fetch_public_page", return_value=None):
            response = self.client.post("/api/evaluate", json={"portfolio": "https://portfolio.example.dev"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(response.json["score"])
        self.assertEqual(response.json["source_status"]["portfolio"]["status"], "unavailable")
        self.assertFalse(response.json["stored"])
        self.assertTrue(response.json["warnings"])

    def test_pdf_is_extracted_in_memory_and_raw_text_is_not_persisted(self):
        marker = "CONFIDENTIAL UNIQUE PDF RESUME TOKEN"
        response = self.client.post(
            "/api/evaluate",
            data={"resume_file": (io.BytesIO(text_pdf(marker + " Python projects experience")), "resume.pdf", "application/pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["modules"]["resume"]["filename"], "resume.pdf")
        self.assertIn("PDF text extracted in memory", response.json["modules"]["resume"]["summary"])
        with portfolio_app.db_connect() as connection:
            stored = connection.execute("SELECT report_json FROM reports WHERE id = ?", (response.json["id"],)).fetchone()[0]
        self.assertNotIn(marker, stored)

    def test_bad_pdf_does_not_block_valid_portfolio(self):
        with patch.object(portfolio_app, "fetch_public_page", return_value=GOOD_PAGE):
            response = self.client.post(
                "/api/evaluate",
                data={"resume_file": (io.BytesIO(b"not a PDF"), "bad.pdf", "application/pdf"), "portfolio": "example.dev"},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(set(response.json["modules"]), {"portfolio"})
        self.assertEqual(response.json["source_status"]["resume"]["status"], "unavailable")
        self.assertTrue(response.json["warnings"])

    def test_invalid_link_does_not_block_resume(self):
        response = self.client.post("/api/evaluate", json={"linkedin": "https://example.com/in/not-linkedin", "resume": "Python project skills"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["modules"]), ["resume"])
        self.assertEqual(response.json["source_status"]["linkedin"]["status"], "unavailable")

    def test_unavailable_github_continues_with_other_signal(self):
        with patch.object(portfolio_app, "github_data", return_value=(None, "Unable to access GitHub.")), patch.object(portfolio_app, "fetch_public_page", return_value=GOOD_PAGE):
            response = self.client.post("/api/evaluate", json={"github": "octocat", "portfolio": "example.dev"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(list(response.json["modules"]), ["portfolio"])
        self.assertIn("Unable to access GitHub", response.json["warnings"][0])

    def test_unavailable_github_alone_returns_informative_no_score_report(self):
        with patch.object(portfolio_app, "github_data", return_value=(None, "Unable to access GitHub.")):
            response = self.client.post("/api/evaluate", json={"github": "octocat"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(response.json["score"])
        self.assertEqual(response.json["modules"], {})
        self.assertFalse(response.json["stored"])
        self.assertTrue(response.json["warnings"])

    def test_linkedin_raw_text_is_not_persisted(self):
        marker = "PRIVATE LINKEDIN PASTE TOKEN"
        text = LINKEDIN_TEXT + marker
        response = self.client.post("/api/evaluate", json={"linkedin_summary": text})
        self.assertEqual(response.status_code, 200, response.json)
        with portfolio_app.db_connect() as connection:
            stored = connection.execute("SELECT report_json FROM reports WHERE id = ?", (response.json["id"],)).fetchone()[0]
        self.assertNotIn(marker, stored)

    def test_private_or_nonstandard_port_page_targets_are_rejected(self):
        with patch.object(portfolio_app.socket, "getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            self.assertFalse(portfolio_app._public_http_target("https://public.example.dev"))
        with patch.object(portfolio_app.socket, "getaddrinfo") as resolver:
            self.assertFalse(portfolio_app._public_http_target("https://public.example.dev:8443"))
            resolver.assert_not_called()

    def test_github_resume_combination_reports_evidence_not_resume_text(self):
        github = {
            "username": "octocat",
            "profile": {"public_repos": 1, "bio": "Developer", "blog": "https://example.dev"},
            "repos": [{"name": "api", "language": "Python", "description": "API", "topics": ["python"], "stargazers_count": 2, "size": 1200, "pushed_at": datetime.now(timezone.utc).isoformat()}],
            "readmes": {"api": {"available": True, "quality_score": 80, "word_count": 120, "sections": 3}},
        }
        resume = "Python Flask experience, projects, skills, education degree. Increased users by 25 percent."
        with patch.object(portfolio_app, "github_data", return_value=(github, None)):
            response = self.client.post("/api/evaluate", json={"github": "octocat", "resume": resume})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(set(response.json["modules"]), {"github", "resume"})
        self.assertTrue(any(skill["name"] == "Python" and len(skill["sources"]) == 2 for skill in response.json["skills"]))
        self.assertEqual(response.json["projects"][0]["name"], "api")
        self.assertIsNotNone(response.json["cross_signal"])
        with portfolio_app.db_connect() as connection:
            stored = connection.execute("SELECT report_json FROM reports WHERE id = ?", (response.json["id"],)).fetchone()[0]
        self.assertNotIn(resume, stored)


if __name__ == "__main__":
    unittest.main()
