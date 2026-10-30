import unittest
from fastapi.testclient import TestClient

import main


class MainApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_root_endpoint_returns_ok(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_health_endpoint_returns_ok(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_build_search_query_includes_positive_and_negative(self) -> None:
        query = main.build_search_query(["crm tool", "sales software"], ["free"], ["saas"])
        self.assertIn('"crm tool"', query)
        self.assertIn('"sales software"', query)
        self.assertIn('-"free"', query)

    def test_build_search_query_falls_back_when_no_keywords(self) -> None:
        query = main.build_search_query([], [], ["saas", "tool"])
        self.assertIn('"saas"', query)

    def test_heuristic_scores_title_matches_highest(self) -> None:
        ctx = {"pos_keywords": ["crm tool"]}
        title_match, _ = main.score_heuristic(
            {"title": "Looking for a CRM tool", "content": ""}, ctx
        )
        no_match, _ = main.score_heuristic(
            {"title": "Random thoughts", "content": "nothing here"}, ctx
        )
        self.assertGreater(title_match, no_match)
        self.assertGreaterEqual(title_match, main.SCORE_THRESHOLD)

    def test_matches_any_is_case_insensitive(self) -> None:
        self.assertTrue(main.matches_any("Looking for a CRM", ["crm"]))
        self.assertFalse(main.matches_any("Looking for a CRM", ["invoice"]))

    def test_strip_html_cleans_markup(self) -> None:
        self.assertEqual(main.strip_html("<p>Hello&nbsp;<b>world</b></p>"), "Hello world")


if __name__ == "__main__":
    unittest.main()