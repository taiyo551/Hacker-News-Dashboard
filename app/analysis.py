import math
import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

STANDARD_MAX_DF = 0.5
STANDARD_MIN_DF = 2
DOMAIN_GENERIC_TERMS = {
    "data", "people", "person", "thing", "things", "system", "systems",
    "company", "companies", "user", "users", "work", "way", "time",
    "year", "years", "article", "news", "source", "show", "ask", "hn", "open",
}
UNRANKABLE_SINGLE_TERMS = {
    "pdf", "html", "video", "audio", "slides", "paper", "guide", "notes",
    "built", "build", "building", "made", "making", "use", "using",
    "new", "good", "major", "low", "high", "just", "more", "most",
}
UNRANKABLE_PHRASE_TOKENS = {
    "pdf", "html", "video", "audio", "slides",
    "built", "made", "using", "just", "good", "major", "low", "high", "new", "more", "most",
}
NER_LABEL_WEIGHTS = {
    "ORG": 1.25,
    "PRODUCT": 1.25,
    "PERSON": 1.05,
    "GPE": 0.95,
    "LOC": 0.85,
    "NORP": 0.85,
    "FAC": 0.85,
    "EVENT": 1.05,
    "WORK_OF_ART": 0.95,
    "LAW": 0.95,
}
NER_FIELD_WEIGHTS = {"title": 3.0, "snippet": 1.5, "comments": 0.5}


@dataclass
class ClusterResult:
    assignments: pd.DataFrame
    summary: pd.DataFrame
    reason: str | None = None


def build_vectorizer(max_df=STANDARD_MAX_DF, min_df=STANDARD_MIN_DF):
    return TfidfVectorizer(
        stop_words="english",
        max_df=max_df,
        min_df=min_df,
        ngram_range=(1, 2),
        max_features=5000,
        strip_accents="unicode",
    )


@lru_cache(maxsize=1)
def load_ner_model():
    try:
        import en_core_web_sm

        return en_core_web_sm.load(disable=["parser", "tagger", "lemmatizer", "textcat"])
    except Exception:
        try:
            import spacy

            return spacy.load("en_core_web_sm", disable=["parser", "tagger", "lemmatizer", "textcat"])
        except Exception:
            return None


def is_meaningful_term(term):
    normalized = str(term or "").strip().lower()
    return bool(normalized) and (" " in normalized or normalized not in DOMAIN_GENERIC_TERMS)


def is_phrase_keyword(value):
    return len(normalize_keyword(value).split()) >= 2


def normalize_keyword(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = text.strip(".,:;!?()[]{}\"'")
    return text


def is_meaningful_entity(value):
    normalized = normalize_keyword(value)
    if not normalized or normalized in DOMAIN_GENERIC_TERMS:
        return False
    if len(normalized) < 2 or len(normalized) > 80:
        return False
    if not re.search(r"[a-z]", normalized):
        return False
    return not normalized.startswith(("http", "www."))


@lru_cache(maxsize=2048)
def detected_entity_label(value):
    normalized = normalize_keyword(value)
    if not normalized:
        return None
    nlp = load_ner_model()
    if nlp is None:
        return None
    candidates = [normalized, normalized.title()]
    for candidate in candidates:
        doc = nlp(candidate)
        for entity in getattr(doc, "ents", []):
            if normalize_keyword(entity.text) == normalized and entity.label_ in NER_LABEL_WEIGHTS:
                return entity.label_
    return None


def is_rankable_keyword(value, entity_words=None):
    normalized = normalize_keyword(value)
    if not normalized:
        return False
    if any(token in UNRANKABLE_PHRASE_TOKENS for token in normalized.split()):
        return False
    if normalized in UNRANKABLE_SINGLE_TERMS and not is_phrase_keyword(normalized):
        return False
    if entity_words is not None:
        return normalized in entity_words or is_phrase_keyword(normalized)
    return is_phrase_keyword(normalized) or detected_entity_label(normalized) is not None


def keyword_quality_score(value):
    normalized = normalize_keyword(value)
    if not is_rankable_keyword(normalized):
        return 0.0
    label = detected_entity_label(normalized)
    if label:
        return 1.35
    return 1.10 if is_phrase_keyword(normalized) else 1.0


def keyword_attention_score(appearance_count, novelty_score, word):
    count = max(float(appearance_count or 0), 0.0)
    novelty = max(float(novelty_score or 0), 0.0)
    quality = keyword_quality_score(word)
    if not count or not quality:
        return 0.0
    novelty_component = 1.0 + math.log1p(max(novelty - 1.0, 0.0))
    if novelty < 1.0:
        novelty_component = max(0.35, novelty)
    return round(math.log1p(count) * novelty_component * quality, 3)


def meaningful_feature_mask(vectorizer):
    return np.array([is_meaningful_term(term) for term in vectorizer.get_feature_names_out()])


def filter_generic_features(vectorizer, matrix):
    mask = meaningful_feature_mask(vectorizer)
    return matrix[:, mask], vectorizer.get_feature_names_out()[mask]


def top_terms(vectorizer, matrix, limit=10):
    if matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return []
    scores = np.asarray(matrix.mean(axis=0)).ravel()
    terms = vectorizer.get_feature_names_out()
    return [
        terms[index] for index in scores.argsort()[::-1]
        if scores[index] > 0 and is_meaningful_term(terms[index])
    ][:limit]


def select_ranked_terms(terms, scores, limit=8, relative_threshold=0.2):
    candidates = [
        (str(terms[index]), float(scores[index]))
        for index in scores.argsort()[::-1]
        if scores[index] > 0 and is_meaningful_term(terms[index])
    ]
    if not candidates:
        return []
    threshold = candidates[0][1] * relative_threshold
    selected = []
    selected_phrase_tokens = set()
    for term, score in candidates:
        if score < threshold or len(selected) >= limit:
            break
        tokens = term.split()
        if len(tokens) == 1 and tokens[0] in selected_phrase_tokens:
            continue
        selected.append({"word": term, "relevance_score": score})
        if len(tokens) > 1:
            selected_phrase_tokens.update(tokens)
    if not selected:
        return []
    max_score = selected[0]["relevance_score"]
    for rank, item in enumerate(selected, 1):
        item["relevance_score"] = round(item["relevance_score"] / max_score, 6)
        item["rank_position"] = rank
    return selected


def rank_entity_keywords(titles, snippets, comments, nlp=None, limit=12):
    if nlp is None:
        nlp = load_ner_model()
    if nlp is None:
        return [[] for _ in titles]

    texts = []
    metadata = []
    field_values = {
        "title": titles,
        "snippet": snippets,
        "comments": comments,
    }
    for field_name, values in field_values.items():
        for article_index, value in enumerate(values):
            text = str(value or "").strip()
            if text:
                texts.append(text[:3000])
                metadata.append((article_index, NER_FIELD_WEIGHTS[field_name]))

    scores_by_article = [dict() for _ in titles]
    if not texts:
        return [[] for _ in titles]

    for doc, (article_index, field_weight) in zip(nlp.pipe(texts), metadata):
        for entity in getattr(doc, "ents", []):
            label = getattr(entity, "label_", "")
            if label not in NER_LABEL_WEIGHTS:
                continue
            word = normalize_keyword(getattr(entity, "text", ""))
            if not is_meaningful_entity(word):
                continue
            phrase_bonus = 1.15 if " " in word else 1.0
            scores_by_article[article_index][word] = (
                scores_by_article[article_index].get(word, 0.0)
                + field_weight * NER_LABEL_WEIGHTS[label] * phrase_bonus
            )

    output = []
    for scores in scores_by_article:
        if not scores:
            output.append([])
            continue
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        max_score = ranked[0][1]
        output.append([
            {"word": word, "relevance_score": round(score / max_score, 6), "rank_position": rank}
            for rank, (word, score) in enumerate(ranked, 1)
        ])
    return output


def merge_keyword_rankings(tfidf_terms, entity_terms, limit=8):
    combined_scores = {}
    entity_words = {normalize_keyword(item["word"]) for item in entity_terms if is_meaningful_entity(item["word"])}
    for item in tfidf_terms:
        word = normalize_keyword(item["word"])
        if is_meaningful_term(word) and is_rankable_keyword(word, entity_words):
            combined_scores[word] = max(combined_scores.get(word, 0.0), float(item["relevance_score"]))
    for item in entity_terms:
        word = normalize_keyword(item["word"])
        if is_meaningful_entity(word):
            combined_scores[word] = max(combined_scores.get(word, 0.0), float(item["relevance_score"]) * 1.35)
    if not combined_scores:
        return []

    ranked = sorted(combined_scores.items(), key=lambda item: (-item[1], item[0]))
    selected = []
    selected_phrase_tokens = set()
    for word, score in ranked:
        tokens = word.split()
        if len(tokens) == 1 and tokens[0] in selected_phrase_tokens:
            continue
        selected.append({"word": word, "relevance_score": score})
        if len(tokens) > 1:
            selected_phrase_tokens.update(tokens)
        if len(selected) >= limit:
            break

    max_score = selected[0]["relevance_score"]
    for rank, item in enumerate(selected, 1):
        item["relevance_score"] = round(item["relevance_score"] / max_score, 6)
        item["rank_position"] = rank
    return selected


def extract_weighted_keywords(titles, snippets, comments, max_df=STANDARD_MAX_DF, min_df=STANDARD_MIN_DF, nlp=None):
    combined = [
        " ".join(filter(None, [str(title or ""), str(snippet or ""), str(comment or "")]))
        for title, snippet, comment in zip(titles, snippets, comments)
    ]
    vectorizer = build_vectorizer(max_df=max_df, min_df=min_df)
    entity_rows = rank_entity_keywords(titles, snippets, comments, nlp=nlp)
    try:
        vectorizer.fit(combined)
        weighted = (
            vectorizer.transform([str(value or "") for value in titles]) * 3.0
            + vectorizer.transform([str(value or "") for value in snippets]) * 1.5
            + vectorizer.transform([str(value or "") for value in comments]) * 0.5
        )
        terms = vectorizer.get_feature_names_out()
        phrase_bonus = np.array([1.25 if " " in term else 1.0 for term in terms])
        tfidf_rows = []
        for row_index in range(weighted.shape[0]):
            scores = np.asarray(weighted[row_index].todense()).ravel() * phrase_bonus
            tfidf_rows.append(select_ranked_terms(terms, scores))
    except ValueError:
        tfidf_rows = [[] for _ in combined]

    output = [
        merge_keyword_rankings(tfidf_terms, entity_terms)
        for tfidf_terms, entity_terms in zip(tfidf_rows, entity_rows)
    ]
    if any(output):
        return output
    raise ValueError("有効なTF-IDF語彙または固有表現がありません")


def cluster_documents(documents, n_clusters=6, max_df=0.8, min_df=2, metadata=None):
    clean = [str(document or "").strip() for document in documents]
    valid_indices = [index for index, document in enumerate(clean) if document]
    if len(valid_indices) < 2:
        return ClusterResult(pd.DataFrame(), pd.DataFrame(), "分析に必要な文書数が不足しています。")
    valid_documents = [clean[index] for index in valid_indices]
    vectorizer = build_vectorizer(max_df=max_df, min_df=min_df)
    try:
        matrix = vectorizer.fit_transform(valid_documents)
    except ValueError:
        return ClusterResult(pd.DataFrame(), pd.DataFrame(), "有効な語彙を抽出できませんでした。")
    if matrix.shape[1] < 2:
        return ClusterResult(pd.DataFrame(), pd.DataFrame(), "クラスタリングに必要な語彙が不足しています。")
    matrix, feature_names = filter_generic_features(vectorizer, matrix)
    if matrix.shape[1] < 2:
        return ClusterResult(pd.DataFrame(), pd.DataFrame(), "クラスタリングに必要な有効語彙が不足しています。")
    cluster_count = max(2, min(int(n_clusters), len(valid_documents), matrix.shape[1]))
    model = KMeans(n_clusters=cluster_count, random_state=42, n_init=10)
    labels = model.fit_predict(matrix)
    dimensions = min(2, matrix.shape[1] - 1, matrix.shape[0] - 1)
    if dimensions >= 2:
        coordinates = TruncatedSVD(n_components=2, random_state=42).fit_transform(matrix)
    else:
        coordinates = np.column_stack([np.arange(len(valid_documents)), np.zeros(len(valid_documents))])
    label_names = {}
    for cluster_id in range(cluster_count):
        center = model.cluster_centers_[cluster_id]
        terms = [feature_names[index] for index in center.argsort()[::-1][:3]]
        label_names[cluster_id] = " / ".join(terms)
    rows = []
    for position, original_index in enumerate(valid_indices):
        row = {"document_index": original_index, "cluster_id": int(labels[position]),
               "cluster_label": label_names[int(labels[position])], "x": coordinates[position, 0],
               "y": coordinates[position, 1], "text": valid_documents[position]}
        if metadata is not None:
            row.update(metadata.iloc[original_index].to_dict())
        rows.append(row)
    assignments = pd.DataFrame(rows)
    summary_rows = []
    for cluster_id, group in assignments.groupby("cluster_id"):
        cluster_matrix = matrix[labels == cluster_id]
        centroid = model.cluster_centers_[cluster_id].reshape(1, -1)
        similarities = cosine_similarity(cluster_matrix, centroid).ravel()
        representative = group.iloc[int(similarities.argmax())]
        summary_rows.append({
            "cluster_id": int(cluster_id),
            "cluster_label": label_names[int(cluster_id)],
            "document_count": len(group),
            "percentage": round(len(group) / len(assignments) * 100, 1),
            "representative_text": representative["text"],
            "average_score": round(float(group["score"].mean()), 1) if "score" in group else None,
        })
    return ClusterResult(assignments, pd.DataFrame(summary_rows).sort_values("document_count", ascending=False))


def related_by_tfidf(documents, target_index, limit=5, max_df=STANDARD_MAX_DF):
    if len(documents) < 2 or target_index < 0 or target_index >= len(documents):
        return []
    vectorizer = build_vectorizer(max_df=max_df, min_df=1)
    try:
        matrix = vectorizer.fit_transform([str(value or "") for value in documents])
    except ValueError:
        return []
    matrix, _ = filter_generic_features(vectorizer, matrix)
    if matrix.shape[1] == 0:
        return []
    scores = cosine_similarity(matrix[target_index], matrix).ravel()
    ranked = [index for index in scores.argsort()[::-1] if index != target_index and scores[index] > 0]
    return [(index, round(float(scores[index]), 4)) for index in ranked[:limit]]
