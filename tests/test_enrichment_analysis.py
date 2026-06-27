import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch"))

from analysis import (build_vectorizer, cluster_documents, extract_weighted_keywords,
                      keyword_attention_score, merge_keyword_rankings, rank_entity_keywords,
                      is_rankable_keyword, related_by_tfidf, select_ranked_terms, top_terms)
from enrich_articles import extract_snippet, is_safe_public_url
from fetch_comments import collect_comments


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, items):
        self.items = items

    def get(self, url, timeout=10):
        return FakeResponse(self.items[int(url.split("/")[-1].split(".")[0])])


class FakeEntity:
    def __init__(self, text, label):
        self.text = text
        self.label_ = label


class FakeDoc:
    def __init__(self, ents):
        self.ents = ents


class FakeNlp:
    def pipe(self, texts):
        for text in texts:
            ents = []
            if "OpenAI" in text:
                ents.append(FakeEntity("OpenAI", "ORG"))
            if "Anthropic" in text:
                ents.append(FakeEntity("Anthropic", "ORG"))
            if "PostgreSQL" in text:
                ents.append(FakeEntity("PostgreSQL", "PRODUCT"))
            yield FakeDoc(ents)


class EnrichmentAnalysisTests(unittest.TestCase):
    def test_snippet_priority(self):
        html = """
        <meta property="og:description" content="Open graph description">
        <meta name="description" content="Meta description">
        <p>This paragraph is deliberately long enough to be considered a valid first article paragraph.</p>
        """
        self.assertEqual(extract_snippet(html, "HN body"), ("Open graph description", "open_graph"))
        self.assertEqual(extract_snippet("<meta name='description' content='Meta description'>", "HN body"),
                         ("Meta description", "meta_description"))
        paragraph = "A" * 90
        self.assertEqual(extract_snippet(f"<p>{paragraph}</p>", "HN body"), (paragraph, "first_paragraph"))
        self.assertEqual(extract_snippet("", "HN body"), ("HN body", "hn_text"))

    def test_private_addresses_are_blocked(self):
        self.assertFalse(is_safe_public_url("http://127.0.0.1/private"))
        self.assertFalse(is_safe_public_url("http://localhost/private"))

    def test_comments_preserve_depth_first_order_and_skip_deleted(self):
        session = FakeSession({
            1: {"id": 1, "by": "one", "text": "<p>First</p>", "kids": [3], "parent": 99},
            2: {"id": 2, "deleted": True, "kids": [4], "parent": 99},
            3: {"id": 3, "by": "reply", "text": "Reply", "parent": 1},
            4: {"id": 4, "by": "visible", "text": "Visible reply", "parent": 2},
        })
        result = collect_comments(session, {"kids": [1, 2]})
        self.assertEqual([item["hn_comment_id"] for item in result], [1, 3, 4])
        self.assertEqual([item["depth"] for item in result], [0, 1, 1])

    def test_cluster_percentages_total_100(self):
        documents = [
            "python security patch vulnerability",
            "python security release vulnerability",
            "browser privacy tracking cookies",
            "browser privacy protection cookies",
        ]
        result = cluster_documents(documents, n_clusters=2, max_df=1.0, min_df=1)
        self.assertIsNone(result.reason)
        self.assertAlmostEqual(float(result.summary["percentage"].sum()), 100.0)
        self.assertEqual(len(result.assignments), 4)

    def test_related_documents_exclude_source(self):
        matches = related_by_tfidf(
            ["python security vulnerability", "python security patch", "gardening flowers soil"], 0, 2, max_df=1.0
        )
        self.assertEqual(matches[0][0], 1)
        self.assertNotIn(0, [index for index, _ in matches])

    def test_metadata_is_preserved_in_clusters(self):
        metadata = pd.DataFrame([{"id": 1, "score": 10}, {"id": 2, "score": 20}])
        result = cluster_documents(["database query sql", "database index sql"], 2, 1.0, 1, metadata)
        self.assertIn("id", result.assignments.columns)
        self.assertIn("average_score", result.summary.columns)

    def test_generic_unigrams_are_removed_but_phrases_remain(self):
        vectorizer = build_vectorizer(max_df=1.0, min_df=1)
        matrix = vectorizer.fit_transform(["data pipeline people personal data system"])
        terms = top_terms(vectorizer, matrix, 20)
        self.assertNotIn("data", terms)
        self.assertNotIn("people", terms)
        self.assertNotIn("system", terms)
        self.assertIn("data pipeline", terms)
        self.assertIn("personal data", terms)

    def test_weighted_keywords_prefer_title_and_limit_results(self):
        results = extract_weighted_keywords(
            ["python security patch"],
            ["python release notes"],
            ["security discussion " + "commentary " * 20],
            max_df=1.0,
            min_df=1,
        )[0]
        self.assertLessEqual(len(results), 8)
        self.assertEqual(results[0]["rank_position"], 1)
        self.assertEqual(results[0]["relevance_score"], 1.0)
        words = [item["word"] for item in results]
        self.assertTrue(any("python" in word or "security" in word for word in words[:3]))

    def test_ner_entities_are_ranked_and_normalized(self):
        results = rank_entity_keywords(
            ["OpenAI releases a PostgreSQL agent"],
            ["Anthropic is mentioned in passing"],
            [""],
            nlp=FakeNlp(),
        )[0]
        words = [item["word"] for item in results]
        self.assertEqual(words[0], "openai")
        self.assertIn("postgresql", words)
        self.assertIn("anthropic", words)

    def test_weighted_keywords_merge_ner_with_tfidf(self):
        results = extract_weighted_keywords(
            ["OpenAI releases a PostgreSQL agent"],
            ["A short note about database tooling"],
            [""],
            max_df=1.0,
            min_df=1,
            nlp=FakeNlp(),
        )[0]
        words = [item["word"] for item in results]
        self.assertIn("openai", words[:3])
        self.assertIn("postgresql", words)
        self.assertNotIn("releases", words)

    def test_merge_keywords_keeps_only_phrases_or_entities(self):
        results = merge_keyword_rankings(
            [
                {"word": "built", "relevance_score": 1.0},
                {"word": "open source", "relevance_score": 0.8},
                {"word": "openai", "relevance_score": 0.2},
            ],
            [{"word": "openai", "relevance_score": 0.9}],
        )
        words = [item["word"] for item in results]
        self.assertNotIn("built", words)
        self.assertIn("open source", words)
        self.assertLess(words.index("openai"), words.index("open source"))

    def test_attention_score_filters_unrankable_single_terms(self):
        self.assertEqual(keyword_attention_score(4, 2.0, "built"), 0.0)
        self.assertFalse(is_rankable_keyword("pdf"))
        self.assertGreater(keyword_attention_score(4, 2.0, "open source"), 0.0)

    def test_field_weights_change_keyword_order(self):
        results = extract_weighted_keywords(
            ["python runtime", "security patch"],
            ["", ""],
            ["security patch", "python runtime"],
            max_df=1.0,
            min_df=1,
        )
        self.assertEqual(results[0][0]["word"], "python runtime")
        self.assertEqual(results[1][0]["word"], "security patch")

    def test_phrase_suppresses_duplicate_unigram_and_threshold(self):
        terms = pd.Series(["data pipeline", "pipeline", "rare", "security"]).to_numpy()
        scores = pd.Series([10.0, 9.0, 1.9, 8.0]).to_numpy()
        results = select_ranked_terms(terms, scores, limit=8, relative_threshold=0.2)
        words = [item["word"] for item in results]
        self.assertIn("data pipeline", words)
        self.assertNotIn("pipeline", words)
        self.assertNotIn("rare", words)


if __name__ == "__main__":
    unittest.main()
