import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch"))

from db import (article_source, calculate_importance, get_article_momentum,
                get_category_momentum, get_keyword_shifts, get_public_articles,
                get_story_clusters)
from extract_keywords import extract_keywords
from fetch_articles import TOP_N, infer_category


class ScoringTests(unittest.TestCase):
    def test_importance_is_bounded_and_orders_articles(self):
        df = pd.DataFrame([
            {"score": 10, "comment_count": 2, "velocity": 1},
            {"score": 100, "comment_count": 20, "velocity": 10},
        ])
        result = calculate_importance(df)
        self.assertTrue(result["importance_score"].between(0, 100).all())
        self.assertGreater(result.iloc[1]["importance_score"], result.iloc[0]["importance_score"])

    def test_importance_handles_empty_frame(self):
        result = calculate_importance(pd.DataFrame())
        self.assertIn("importance_score", result.columns)
        self.assertTrue(result.empty)

    def test_keyword_normalization_and_stopwords(self):
        result = extract_keywords("Using JavaScript with PostgreSQL and K8s")
        self.assertNotIn("using", result)
        self.assertTrue({"js", "postgres", "kubernetes"}.issubset(result))

    def test_fallback_keyword_extraction_removes_domain_generic_terms(self):
        result = extract_keywords("Data people system Python")
        self.assertNotIn("data", result)
        self.assertNotIn("people", result)
        self.assertNotIn("system", result)
        self.assertIn("python", result)

    def test_empty_article_result_does_not_require_sort_column(self):
        from unittest.mock import patch

        with patch("db.fetch_df", return_value=pd.DataFrame()):
            result = get_public_articles(7, sort_by="comments")
        self.assertTrue(result.empty)
        self.assertIn("comment_count", result.columns)

    def test_and_search_adds_one_clause_per_term(self):
        from unittest.mock import patch

        with patch("db.fetch_df", return_value=pd.DataFrame()) as fetch:
            get_public_articles(7, search_text="python security")
        sql, params = fetch.call_args.args
        self.assertEqual(sql.count("EXISTS (SELECT 1 FROM article_keywords"), 2)
        self.assertEqual(params.count("%python%"), 5)
        self.assertEqual(params.count("%security%"), 5)

    def test_public_article_query_returns_public_columns_when_empty(self):
        from unittest.mock import patch

        with patch("db.fetch_df", return_value=pd.DataFrame()) as fetch:
            result = get_public_articles(7, search_text="python")
        _, params = fetch.call_args.args
        self.assertTrue(result.empty)
        self.assertIn("comment_count", result.columns)
        self.assertEqual(params.count("%python%"), 5)

    def test_fetches_top_500(self):
        self.assertEqual(TOP_N, 500)

    def test_extended_category_classification(self):
        self.assertEqual(infer_category("Grit: Rewriting Git in Rust", "story"), "Programming")
        self.assertEqual(infer_category("A giant star explodes", "story"), "Science")
        self.assertEqual(infer_category("FCC proposes telecom regulation", "story"), "Policy")

    def test_article_source_normalizes_domain(self):
        self.assertEqual(article_source("https://www.example.com/story"), "example.com")

    def test_story_clusters_group_related_titles(self):
        from unittest.mock import patch

        articles = pd.DataFrame([
            {"id": 1, "title": "Python security release fixes major issue", "url": "https://a.test/1",
             "category": "Security", "score": 100, "comment_count": 10, "posted_at": "2026-06-10", "keywords": "python,security,release"},
            {"id": 2, "title": "Major Python security release announced", "url": "https://b.test/2",
             "category": "Security", "score": 80, "comment_count": 5, "posted_at": "2026-06-10", "keywords": "python,security,release"},
            {"id": 3, "title": "A completely different database story", "url": "https://c.test/3",
             "category": "Database", "score": 50, "comment_count": 2, "posted_at": "2026-06-10", "keywords": "database,story"},
        ])
        with patch("db.fetch_df", return_value=articles):
            clusters = get_story_clusters(7)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(int(clusters.iloc[0]["source_count"]), 2)

    def test_momentum_prefers_observed_velocity_and_sets_confidence(self):
        from unittest.mock import patch

        articles = pd.DataFrame([
            {"id": 1, "title": "Observed", "url": "", "category": "AI", "score": 100, "comment_count": 10,
             "posted_at": "2026-06-10", "age_hours": 4, "snapshot_count": 3, "observed_velocity": 80},
            {"id": 2, "title": "Early", "url": "", "category": "Web", "score": 100, "comment_count": 10,
             "posted_at": "2026-06-10", "age_hours": 4, "snapshot_count": 1, "observed_velocity": None},
        ])
        with patch("db.fetch_df", return_value=articles):
            result = get_article_momentum()
        self.assertEqual(result.iloc[0]["title"], "Observed")
        self.assertEqual(result.iloc[0]["confidence"], "高")
        self.assertEqual(result.iloc[1]["momentum_basis"], "投稿後の反応速度")

    def test_category_momentum_detects_acceleration(self):
        from unittest.mock import patch

        data = pd.DataFrame([
            {"category": "AI", "current_count": 10, "previous_count": 2,
             "current_engagement": 100, "previous_engagement": 50},
            {"category": "Web", "current_count": 1, "previous_count": 5,
             "current_engagement": 20, "previous_engagement": 40},
        ])
        with patch("db.fetch_df", return_value=data):
            result = get_category_momentum()
        self.assertEqual(result.iloc[0]["category"], "AI")
        self.assertGreater(result.iloc[0]["momentum"], 0)
        self.assertLess(result.iloc[1]["momentum"], 0)

    def test_keyword_shifts_classify_growth_and_decline(self):
        from unittest.mock import patch

        data = pd.DataFrame([
            {"word": "agent", "first_seen_at": "2026-06-01", "current_count": 8, "previous_count": 1, "novelty_score": 4},
            {"word": "legacy", "first_seen_at": "2026-05-01", "current_count": 1, "previous_count": 8, "novelty_score": 1},
        ])
        with patch("db.fetch_df", return_value=data):
            result = get_keyword_shifts()
        directions = dict(zip(result["word"], result["direction"]))
        self.assertEqual(directions["agent"], "急上昇")
        self.assertEqual(directions["legacy"], "減速")


if __name__ == "__main__":
    unittest.main()
