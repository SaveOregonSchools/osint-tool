import unittest
from unittest.mock import patch

import requests

from providers.web_inspector import (
    PageParser,
    UnsafeUrlError,
    WebInspector,
    is_connection_failure,
    redact_url,
    validate_public_url,
    www_fallback_url,
)
from queries import web_page_inspector as inspector_query
from queries.web_page_inspector import _row, _safe_form_for_storage, iter_row_dicts, render_fields


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}

    def get(self, url, **kwargs):
        self.response.url = url
        return self.response


def html_response(body: str, *, headers=None) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers.update(headers or {"Content-Type": "text/html; charset=utf-8"})
    response._content = body.encode("utf-8")
    response._content_consumed = True
    response.encoding = "utf-8"
    response.raw = type("Raw", (), {"headers": type("Headers", (), {"getlist": lambda self, key: []})()})()
    return response


class SequenceSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}

    def get(self, url, **kwargs):
        response = next(self.responses)
        response.url = url
        return response


class WebInspectorTests(unittest.TestCase):
    def test_browser_mode_is_the_default(self):
        fields = render_fields({})
        self.assertIn('value="browser" selected', fields)

    def test_url_validation_blocks_private_and_credentials(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://127.0.0.1/")
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://user:password@example.org/", resolve=False)
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("https://example.org:8443/", resolve=False)

    @patch("providers.web_inspector._public_addresses", return_value=["93.184.216.34"])
    def test_url_validation_accepts_public_web_url(self, _resolve):
        self.assertEqual(validate_public_url("example.org/path?x=1"), "https://example.org/path?x=1")

    def test_redact_url_preserves_names_not_values(self):
        result = redact_url("https://example.org/collect?email=person%40example.org&campaign=summer#fragment")
        self.assertIn("email=%3Credacted%3E", result)
        self.assertIn("campaign=%3Credacted%3E", result)
        self.assertNotIn("person", result)
        self.assertNotIn("fragment", result)

    def test_www_fallback_preserves_path_and_query(self):
        self.assertEqual(
            www_fallback_url("https://beaverton.k12.or.us/about?section=news"),
            "https://www.beaverton.k12.or.us/about?section=news",
        )
        self.assertIsNone(www_fallback_url("https://www.example.org/"))
        self.assertIsNone(www_fallback_url("https://192.0.2.1/"))

    def test_connection_failure_detection_excludes_http_status_errors(self):
        self.assertTrue(is_connection_failure(requests.ConnectTimeout("connection timed out")))
        self.assertTrue(is_connection_failure(RuntimeError("Page.goto: net::ERR_CONNECTION_TIMED_OUT")))
        self.assertFalse(is_connection_failure(requests.HTTPError("403 Client Error")))

    def test_query_values_are_not_persisted_in_rows_or_run_parameters(self):
        target = "https://example.org/page?email=person%40example.org"
        item = {"category": "page", "name": "Example", "url": redact_url(target)}
        row = _row(target, target, item, source_api="test")
        safe_form = _safe_form_for_storage({"urls": target, "render_page": "on"})
        self.assertNotIn("person", row["target_input"])
        self.assertNotIn("person", safe_form["urls"])
        self.assertIn("email=%3Credacted%3E", safe_form["urls"])

    @patch("queries.web_page_inspector.rendered_findings")
    @patch("queries.web_page_inspector.WebInspector")
    def test_browser_still_runs_when_static_request_is_forbidden(self, inspector_class, rendered):
        inspector_class.return_value.static_findings.side_effect = requests.HTTPError(
            "403 Client Error: Forbidden for url: https://friendsofpfa.org/"
        )
        rendered.return_value = [
            {"category": "rendered_page", "name": "Friends of PFA", "url": "https://friendsofpfa.org/"}
        ]
        rows = list(iter_row_dicts({"urls": "https://friendsofpfa.org/", "inspection_mode": "browser"}))
        self.assertEqual([row["category"] for row in rows], ["error", "rendered_page"])
        rendered.assert_called_once()

    @patch("queries.web_page_inspector.rendered_findings")
    @patch("queries.web_page_inspector.WebInspector")
    def test_connection_timeout_retries_www_and_uses_it_for_browser(self, inspector_class, rendered):
        fallback_url = "https://www.beaverton.k12.or.us/"
        inspector_class.return_value.static_findings.side_effect = [
            requests.ConnectTimeout("Connection to beaverton.k12.or.us timed out"),
            (
                fallback_url,
                [{"category": "page", "name": "Beaverton School District", "url": fallback_url}],
            ),
        ]
        rendered.return_value = [{"category": "rendered_page", "name": "District", "url": fallback_url}]

        rows = list(iter_row_dicts({"urls": "https://beaverton.k12.or.us", "inspection_mode": "browser"}))

        self.assertEqual([row["category"] for row in rows], ["hostname_fallback", "page", "rendered_page"])
        self.assertEqual(rows[0]["target_input"], "https://beaverton.k12.or.us")
        self.assertIn("No redirect", rows[0]["evidence"])
        self.assertEqual(
            [call.args[0] for call in inspector_class.return_value.static_findings.call_args_list],
            ["https://beaverton.k12.or.us", fallback_url],
        )
        rendered.assert_called_once_with(fallback_url, observe_seconds=3.0, max_requests=500)

    @patch("providers.web_inspector.validate_public_url")
    def test_redirect_destination_is_revalidated(self, validate):
        validate.side_effect = ["https://example.org/", UnsafeUrlError("private redirect")]
        redirect = requests.Response()
        redirect.status_code = 302
        redirect.headers["Location"] = "http://127.0.0.1/admin"
        redirect.raw = type("Raw", (), {"close": lambda self: None})()
        inspector = WebInspector(session=SequenceSession([redirect]))
        with self.assertRaises(UnsafeUrlError):
            inspector.fetch("https://example.org/")
        self.assertEqual(validate.call_count, 2)

    @patch("providers.web_inspector.validate_public_url", side_effect=lambda value: value)
    def test_fetch_decodes_meta_charset_without_consuming_stream_twice(self, _validate):
        response = html_response('<meta charset="windows-1252"><title>Caf\xe9</title>')
        response._content = '<meta charset="windows-1252"><title>Caf\xe9</title>'.encode("windows-1252")
        response.encoding = None
        inspector = WebInspector(session=FakeSession(response))
        _response, body = inspector.fetch("https://example.org/")
        self.assertIn("Caf\xe9", body)

    def test_page_parser_collects_resources_forms_and_json_ld(self):
        parser = PageParser("https://example.org/about")
        parser.feed(
            """
            <html><head><title>Example Organization</title>
            <meta name="generator" content="WordPress 6">
            <script src="https://cdn.example.net/jquery.min.js"></script>
            <script type="application/ld+json">{"@type":"Organization","name":"Example"}</script>
            </head><body><a href="https://partner.example.net/about">Partner</a>
            <form method="post" action="https://forms.example.net/submit">
            <input name="email" type="email"><input name="secret" type="password"></form></body></html>
            """
        )
        self.assertEqual(parser.page.title, "Example Organization")
        self.assertEqual(parser.page.resources[0][0], "script")
        self.assertEqual(parser.page.forms[0]["method"], "POST")
        self.assertEqual(parser.page.forms[0]["fields"][0]["name"], "email")
        self.assertEqual(parser.page.json_ld[0]["@type"], "Organization")
        self.assertEqual(parser.page.links, ["https://partner.example.net/about"])

    @patch("providers.web_inspector.validate_public_url", side_effect=lambda value: value)
    def test_static_findings_detect_technology_and_third_party(self, _validate):
        response = html_response(
            """
            <html><head><title>Example</title><meta name="generator" content="WordPress 6.8">
            <script src="https://www.googletagmanager.com/gtm.js?id=GTM-TEST"></script>
            <script>window.localStorage.setItem('preference', 'yes');</script>
            </head><body><form action="https://forms.vendor.test/submit"><input name="email" type="email"></form></body></html>
            """,
            headers={"Content-Type": "text/html", "Server": "ExampleServer"},
        )
        inspector = WebInspector(session=FakeSession(response))
        _url, findings = inspector.static_findings("https://example.org/")
        technologies = {item["name"] for item in findings if item["category"] == "technology"}
        destinations = {item["name"] for item in findings if item["category"] == "third_party_destination"}
        behaviors = {item["name"] for item in findings if item["category"] == "script_behavior"}
        self.assertIn("WordPress", technologies)
        self.assertIn("Google Tag Manager", technologies)
        self.assertIn("www.googletagmanager.com", destinations)
        self.assertIn("localStorage", behaviors)

    def test_dashboard_summarizes_investigator_signals(self):
        def result_row(**values):
            row = {header: "" for header in inspector_query.HEADERS}
            row.update({"target_input": "https://example.org/", "finding_url": "https://example.org/", **values})
            return [row[header] for header in inspector_query.HEADERS]

        rows = [
            result_row(category="rendered_page", finding_name="Example Organization"),
            result_row(
                category="hostname_fallback",
                finding_name="Tried www hostname after connection failure",
                finding_url="https://www.example.org/",
                host="www.example.org",
            ),
            result_row(category="rendered_technology", finding_name="WordPress", details='{"type":"CMS","confidence":"high"}'),
            result_row(category="browser_cookie", finding_name="_ga", details='{"secure":false}'),
            result_row(category="browser_cookie", finding_name="session", details='{"secure":true}'),
            result_row(
                category="network_request",
                finding_name="script",
                finding_url="https://www.google-analytics.com/g/collect",
                host="www.google-analytics.com",
                third_party="yes",
                details='{"method":"POST"}',
            ),
            result_row(
                category="rendered_linked_domain",
                finding_name="agency.gov",
                finding_url="https://agency.gov/",
                host="agency.gov",
                third_party="yes",
                details='{"link_count":3}',
            ),
            result_row(
                category="rendered_wordpress_theme",
                finding_name="twentytwentyfive",
                details='{"slug":"twentytwentyfive"}',
            ),
        ]
        html = inspector_query.render_results({}, inspector_query.HEADERS, rows)
        self.assertIn("Website intelligence summary", html)
        self.assertIn("WordPress", html)
        self.assertIn("Twenty Twenty-Five", html)
        self.assertIn("Cookies", html)
        self.assertIn("agency.gov", html)
        self.assertIn("Google Analytics", html)
        self.assertIn("inspector fallback, not an observed redirect", html)
        self.assertIn("Detailed evidence (8 rows)", html)


if __name__ == "__main__":
    unittest.main()
