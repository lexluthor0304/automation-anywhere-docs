import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "search_docs.py"
SPEC = importlib.util.spec_from_file_location("search_docs", SCRIPT)
search_docs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(search_docs)


class SearchDocsTests(unittest.TestCase):
    def test_html_to_text_removes_markup_and_preserves_list_items(self):
        source = (
            "<div><p>Hello <strong>world</strong>.</p><ul><li>One</li>"
            "<li>Two</li></ul><script>bad()</script></div>"
        )
        text = search_docs.html_to_text(source)
        self.assertIn("Hello world.", text)
        self.assertIn("- One", text)
        self.assertIn("- Two", text)
        self.assertNotIn("bad", text)

    def test_relevant_excerpt_selects_matching_block(self):
        text = (
            "Introduction with unrelated material. " * 20
            + "\n\nOAuth bearer token is supported for this Control Room endpoint."
            + "\n\nAnother unrelated appendix. " * 20
        )
        excerpt = search_docs.relevant_excerpt(
            text, "Control Room OAuth bearer token", 240
        )
        self.assertIn("OAuth bearer token", excerpt)
        self.assertLessEqual(len(excerpt), 241)

    def test_parse_results_deduplicates_pretty_url_and_keeps_newest(self):
        def entry(map_id, content_id, published):
            return {
                "type": "TOPIC",
                "topic": {
                    "mapId": map_id,
                    "contentId": content_id,
                    "title": "Authentication API",
                    "breadcrumb": None,
                    "readerUrl": "https://docs.automationanywhere.com/r/control-room-apis/auth-api",
                    "contentUrl": "https://docs.automationanywhere.com/api/khub/maps/{}/topics/{}/content".format(
                        map_id, content_id
                    ),
                    "metadata": [
                        {
                            "key": "ft:prettyUrl",
                            "values": ["control-room-apis/auth-api"],
                        },
                        {"key": "ft:lastPublication", "values": [published]},
                    ],
                },
            }

        response = {
            "results": [
                {"entries": [entry("old", "one", "2025-01-01T00:00:00")]},
                {"entries": [entry("new", "two", "2026-01-01T00:00:00")]},
            ]
        }
        results = search_docs.parse_search_results(response)
        self.assertEqual(1, len(results))
        self.assertIn("/maps/new/", results[0]["content_url"])
        self.assertEqual([], results[0]["breadcrumb"])

    def test_non_numeric_total_count_defaults_to_zero(self):
        response = {"paging": {"totalResultsCount": "unknown"}, "results": []}
        with mock.patch.object(search_docs, "_request_json", return_value=response):
            results, total = search_docs.search(
                "query", locale="en-US", pool_size=20, timeout=1
            )
        self.assertEqual([], results)
        self.assertEqual(0, total)

    def test_rejects_non_official_urls(self):
        with self.assertRaises(search_docs.DocsError):
            search_docs.validate_official_url("https://example.com/fake-docs")
        with self.assertRaises(search_docs.DocsError):
            search_docs.validate_official_url(
                "https://docs.automationanywhere.com:8443/private"
            )

    def test_redirects_are_disabled(self):
        handler = search_docs._NoRedirectHandler()
        request = search_docs.urllib.request.Request(
            "https://docs.automationanywhere.com/start"
        )
        with self.assertRaises(search_docs.urllib.error.URLError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {"Location": "https://example.com/redirect"},
                "https://example.com/redirect",
            )

    def test_canonical_url_removes_navigation_query(self):
        url = "https://docs.automationanywhere.com/r/control-room-apis/v4?tocId=abc#section"
        self.assertEqual(
            "https://docs.automationanywhere.com/r/control-room-apis/v4",
            search_docs.canonical_official_url(url),
        )

    def test_public_result_removes_internal_fields(self):
        value = {
            "title": "A",
            "url": "https://docs.automationanywhere.com/r/a",
            "content_url": "https://docs.automationanywhere.com/api/khub/a",
            "published": "2026-01-01",
            "snippet": "x",
            "unique_key": "a",
            "text": "evidence",
        }
        public = search_docs.public_result(value)
        self.assertEqual({"title", "url", "text"}, set(public))

    def test_missing_content_endpoint_is_disclosed(self):
        text, warning = search_docs._fetch_content(
            {"snippet": "Search-only evidence", "content_url": ""},
            "evidence",
            300,
            1,
        )
        self.assertEqual("Search-only evidence", text)
        self.assertIn("search excerpt", warning)

    def test_empty_query_is_rejected_before_network_access(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                search_docs.main([""])
        self.assertEqual(2, caught.exception.code)

    def test_stdin_query_is_read_without_shell_interpolation(self):
        dangerous = "Control Room error $(touch /tmp/not-run) `whoami`"
        parser = search_docs.build_parser()
        args = parser.parse_args(["--stdin"])
        with mock.patch.object(search_docs.sys, "stdin", io.StringIO(dangerous + "\n")):
            query = search_docs.sys.stdin.readline(1002).strip()
        self.assertTrue(args.read_stdin)
        self.assertEqual(dangerous, query)

    def test_json_output_honors_character_limit(self):
        payload = {
            "query": "q",
            "retrieved_at": "2026-09-01T00:00:00+00:00",
            "source": search_docs.SEARCH_URL,
            "total_matches": 3,
            "warnings": [],
            "results": [
                {
                    "title": "Result {}".format(index),
                    "url": "https://docs.automationanywhere.com/r/result-{}".format(
                        index
                    ),
                    "breadcrumb": ["A", "B", "C"],
                    "text": "evidence " * 1000,
                }
                for index in range(3)
            ],
        }
        rendered = search_docs.render_json(payload, 2000)
        self.assertLessEqual(len(rendered), 2000)
        parsed = json.loads(rendered)
        self.assertTrue(parsed["results"])

        payload["total_matches"] = 10**5000
        rendered = search_docs.render_json(payload, 2000)
        self.assertLessEqual(len(rendered), 2000)
        json.loads(rendered)


if __name__ == "__main__":
    unittest.main()
