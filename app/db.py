import math
import os
import re
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv

from analysis import DOMAIN_GENERIC_TERMS

load_dotenv()

PUBLIC_ARTICLE_COLUMNS = [
    "id", "hn_id", "title", "url", "author", "type", "category", "score",
    "comment_count", "summary_ja", "is_summarized", "posted_at", "fetched_at",
    "snippet", "snippet_source", "content_fetch_status",
    "keyword_list", "velocity", "importance_score",
]

ACTIVE_BATCH_JOBS = (
    "fetch_articles",
    "record_snapshots",
    "extract_keywords",
    "summarize_articles",
    "enrich_articles",
    "fetch_comments",
)


def _config_value(name: str, default=None):
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    try:
        import streamlit as st

        if name in st.secrets:
            return st.secrets[name]
        database = st.secrets.get("database", {})
        return database.get(name, default)
    except Exception:
        return default


def get_connection():
    import mysql.connector

    return mysql.connector.connect(
        host=_config_value("DB_HOST", "localhost"),
        port=int(_config_value("DB_PORT", "3306")),
        user=_config_value("DB_USER", "root"),
        password=_config_value("DB_PASSWORD", ""),
        database=_config_value("DB_NAME", "hn_dashboard"),
    )


def get_sqlite_path() -> Path:
    configured = _config_value("SQLITE_DB_PATH")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data" / "hn_dashboard.sqlite"


def get_public_connection():
    path = get_sqlite_path()
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_df(sql: str, params=None) -> pd.DataFrame:
    conn = get_public_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return pd.DataFrame([dict(row) for row in cur.fetchall()])
    finally:
        conn.close()


def _public_as_of_sql() -> str:
    return "(SELECT COALESCE(MAX(COALESCE(fetched_at, posted_at)), CURRENT_TIMESTAMP) FROM articles)"


def _public_snapshot_as_of_sql() -> str:
    return f"(SELECT COALESCE(MAX(recorded_at), {_public_as_of_sql()}) FROM article_snapshots)"


def _public_stat_date_sql(table: str) -> str:
    return f"(SELECT COALESCE(MAX(stat_date), date({_public_as_of_sql()})) FROM {table})"


def get_trending_keyword_count(threshold: float = 2.0) -> int:
    df = fetch_df(
        f"""SELECT COUNT(*) count
            FROM keyword_daily_stats
            WHERE stat_date={_public_stat_date_sql("keyword_daily_stats")} AND novelty_score>?""",
        [threshold],
    )
    return int(df.iloc[0]["count"] or 0) if not df.empty else 0


def get_latest_batch_runs() -> pd.DataFrame:
    placeholders = ",".join(["?"] * len(ACTIVE_BATCH_JOBS))
    return fetch_df(
        f"""SELECT br.* FROM batch_runs br
            JOIN (
              SELECT job_name, MAX(id) id FROM batch_runs
              WHERE job_name IN ({placeholders})
              GROUP BY job_name
            ) latest ON latest.id=br.id
            ORDER BY br.job_name""",
        list(ACTIVE_BATCH_JOBS),
    )


def get_recent_batch_runs(limit: int = 30) -> pd.DataFrame:
    placeholders = ",".join(["?"] * len(ACTIVE_BATCH_JOBS))
    return fetch_df(
        f"""SELECT job_name,status,started_at,finished_at,processed_count,success_count,failure_count,error_message
            FROM batch_runs
            WHERE job_name IN ({placeholders})
            ORDER BY id DESC LIMIT ?""",
        [*ACTIVE_BATCH_JOBS, limit],
    )


def get_pipeline_health(days: int = 7) -> dict:
    df = fetch_df(
        f"""SELECT COUNT(DISTINCT a.id) article_count,
                  COUNT(DISTINCT ak.article_id) keyword_article_count,
                  COUNT(DISTINCT CASE WHEN a.is_summarized=1 THEN a.id END) summarized_count,
                  COUNT(DISTINCT CASE WHEN COALESCE(a.category,'Other')='Other' THEN a.id END) other_count,
                  COUNT(DISTINCT snapshots.article_id) snapshot_article_count,
                  COUNT(DISTINCT CASE WHEN ac.snippet IS NOT NULL AND ac.snippet<>'' THEN a.id END) snippet_count,
                  COUNT(DISTINCT comments.article_id) comment_article_count,
                  COUNT(DISTINCT CASE WHEN ac.fetch_status IN ('failed','blocked') THEN a.id END) content_failure_count
           FROM articles a
           LEFT JOIN article_keywords ak ON ak.article_id=a.id
           LEFT JOIN (SELECT DISTINCT article_id FROM article_snapshots) snapshots ON snapshots.article_id=a.id
           LEFT JOIN article_content ac ON ac.article_id=a.id
           LEFT JOIN (SELECT DISTINCT article_id FROM article_comments) comments ON comments.article_id=a.id
           WHERE a.posted_at>=datetime({_public_as_of_sql()}, ?)""",
        [f"-{days} days"],
    )
    if df.empty:
        return {"article_count": 0, "keyword_coverage": 0, "summary_coverage": 0, "classified_coverage": 0,
                "snapshot_coverage": 0, "snippet_coverage": 0, "comment_coverage": 0, "content_failure_count": 0}
    row = df.iloc[0]
    total = int(row["article_count"] or 0)
    percent = lambda value: round(int(value or 0) / total * 100, 1) if total else 0
    return {
        "article_count": total,
        "keyword_coverage": percent(row["keyword_article_count"]),
        "summary_coverage": percent(row["summarized_count"]),
        "classified_coverage": round(100 - percent(row["other_count"]), 1),
        "snapshot_coverage": percent(row["snapshot_article_count"]),
        "snippet_coverage": percent(row["snippet_count"]),
        "comment_coverage": percent(row["comment_article_count"]),
        "content_failure_count": int(row["content_failure_count"] or 0),
    }


def get_article_by_id(article_id: int):
    df = fetch_df(
        """SELECT a.*,ac.snippet,ac.snippet_source,ac.hn_text,ac.fetch_status content_fetch_status,
                  ac.fetched_at content_fetched_at,ac.error_message content_error
           FROM articles a LEFT JOIN article_content ac ON ac.article_id=a.id WHERE a.id=?""",
        [article_id],
    )
    return None if df.empty else df.iloc[0].to_dict()


def _article_search_clauses(category: str, article_type: str, search_text: str) -> tuple[list[str], list]:
    where = []
    params = []
    if category != "All":
        where.append("a.category=?")
        params.append(category)
    if article_type != "All":
        where.append("a.type=?")
        params.append(article_type)
    for term in search_text.split():
        like = f"%{term}%"
        where.append("""(a.title LIKE ? OR a.author LIKE ? OR a.category LIKE ? OR a.url LIKE ? OR
                         EXISTS (SELECT 1 FROM article_keywords sak JOIN keywords sk ON sk.id=sak.keyword_id
                                 WHERE sak.article_id=a.id AND sk.word LIKE ?))""")
        params.extend([like] * 5)
    return where, params


def _public_article_query(days: int, category: str, article_type: str, min_score: int,
                          search_text: str, min_comments: int = 0):
    where = [
        f"a.posted_at >= datetime({_public_as_of_sql()}, ?)",
        "a.score >= ?",
        "a.comment_count >= ?",
    ]
    params = [f"-{days} days", min_score, min_comments]
    search_where, search_params = _article_search_clauses(category, article_type, search_text)
    where.extend(search_where)
    params.extend(search_params)
    return f"""
        SELECT a.*, ac.snippet,ac.snippet_source,ac.fetch_status content_fetch_status,
               COALESCE(kws.keyword_list,'') keyword_list,
               COALESCE(v.velocity,0) velocity
        FROM articles a
        LEFT JOIN article_content ac ON ac.article_id=a.id
        LEFT JOIN (
          SELECT article_id,GROUP_CONCAT(word, ', ') keyword_list
          FROM (
            SELECT ak.article_id,k.word
            FROM article_keywords ak JOIN keywords k ON k.id=ak.keyword_id
            WHERE ak.rank_position<=5 ORDER BY ak.article_id,ak.rank_position,k.word
          ) ranked_keywords GROUP BY article_id
        ) kws ON kws.article_id=a.id
        LEFT JOIN (
          SELECT article_id, MAX(MAX(score)-MIN(score),0)+MAX(MAX(comment_count)-MIN(comment_count),0) velocity
          FROM article_snapshots
          WHERE recorded_at >= datetime({_public_snapshot_as_of_sql()}, '-24 hours')
          GROUP BY article_id
        ) v ON v.article_id=a.id
        WHERE {' AND '.join(where)}
    """, params


def calculate_importance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        df["importance_score"] = pd.Series(dtype=float)
        return df
    result = df.copy()
    weights = {"score": 0.45, "comment_count": 0.30, "velocity": 0.25}
    total = pd.Series(0.0, index=result.index)
    for col, weight in weights.items():
        values = pd.to_numeric(result.get(col, 0), errors="coerce").fillna(0).clip(lower=0)
        max_value = float(values.max())
        normalized = values / max_value if max_value > 0 else values * 0
        total += normalized * weight
    result["importance_score"] = (total * 100).round(1).clip(0, 100)
    return result


def get_public_articles(days: int, category="All", article_type="All", min_score=0,
                        search_text="", sort_by="importance", limit=100, min_comments=0):
    sql, params = _public_article_query(days, category, article_type, min_score, search_text, min_comments)
    df = calculate_importance(fetch_df(sql, params))
    if df.empty:
        return df.reindex(columns=PUBLIC_ARTICLE_COLUMNS)
    sort_map = {"new": "posted_at", "score": "score", "comments": "comment_count",
                "rising": "velocity", "importance": "importance_score"}
    sort_column = sort_map.get(sort_by, "importance")
    if sort_column not in df.columns:
        return df.reindex(columns=PUBLIC_ARTICLE_COLUMNS)
    return df.sort_values(sort_column, ascending=False).head(limit).reindex(columns=PUBLIC_ARTICLE_COLUMNS)


def article_source(url: str | None) -> str:
    if not url:
        return "news.ycombinator.com"
    source = urlparse(str(url)).netloc.lower()
    return source.removeprefix("www.") or "news.ycombinator.com"


def get_story_clusters(days: int = 7, limit: int = 8) -> pd.DataFrame:
    articles = fetch_df(
        """SELECT a.id,a.title,a.url,a.category,a.score,a.comment_count,a.posted_at,
                  GROUP_CONCAT(DISTINCT k.word) keywords
           FROM articles a
           LEFT JOIN article_keywords ak ON ak.article_id=a.id
           LEFT JOIN keywords k ON k.id=ak.keyword_id
           WHERE a.posted_at>=datetime({as_of}, ?)
           GROUP BY a.id ORDER BY a.score DESC LIMIT 400""".format(as_of=_public_as_of_sql()),
        [f"-{days} days"],
    )
    columns = ["lead_id", "lead_title", "lead_url", "category", "article_count", "source_count",
               "sources", "total_score", "total_comments", "coverage_score", "titles"]
    if articles.empty:
        return pd.DataFrame(columns=columns)

    ignored = {"show", "ask", "news", "launch", "open", "source", "hn"}
    records = []
    for _, row in articles.iterrows():
        tokens = _tokens(row.get("title", "")) - ignored
        keywords = set(str(row.get("keywords") or "").split(",")) - ignored - {""}
        records.append({"row": row, "tokens": tokens, "keywords": keywords})

    clusters: list[list[dict]] = []
    for record in records:
        best_cluster = None
        best_similarity = 0.0
        for cluster in clusters:
            representative = cluster[0]
            token_union = record["tokens"] | representative["tokens"]
            title_similarity = len(record["tokens"] & representative["tokens"]) / len(token_union) if token_union else 0
            shared_keywords = len(record["keywords"] & representative["keywords"])
            same_category = record["row"].get("category") == representative["row"].get("category")
            similarity = title_similarity + min(shared_keywords, 4) * 0.12 + (0.05 if same_category else 0)
            if (title_similarity >= 0.28 or shared_keywords >= 3) and similarity > best_similarity:
                best_cluster, best_similarity = cluster, similarity
        if best_cluster is None:
            clusters.append([record])
        else:
            best_cluster.append(record)

    output = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        rows = [item["row"] for item in cluster]
        sources = sorted({article_source(row.get("url")) for row in rows})
        lead = max(rows, key=lambda row: int(row.get("score") or 0))
        total_score = sum(int(row.get("score") or 0) for row in rows)
        total_comments = sum(int(row.get("comment_count") or 0) for row in rows)
        output.append({
            "lead_id": int(lead["id"]),
            "lead_title": lead.get("title"),
            "lead_url": lead.get("url"),
            "category": lead.get("category") or "Other",
            "article_count": len(rows),
            "source_count": len(sources),
            "sources": ", ".join(sources),
            "total_score": total_score,
            "total_comments": total_comments,
            "coverage_score": len(sources) * 25 + len(rows) * 10 + math.log1p(total_score) * 5,
            "titles": " / ".join(str(row.get("title") or "") for row in rows),
        })
    return pd.DataFrame(output, columns=columns).sort_values("coverage_score", ascending=False).head(limit)


def get_article_momentum(hours: int = 24, limit: int = 10) -> pd.DataFrame:
    df = fetch_df(
        """SELECT a.id,a.title,a.url,a.category,a.score,a.comment_count,a.posted_at,
                  (julianday({as_of})-julianday(a.posted_at))*24.0 age_hours,
                  COUNT(s.id) snapshot_count,
                  CASE WHEN COUNT(s.id)>=2 THEN
                    (MAX(s.score)-MIN(s.score)+MAX(s.comment_count)-MIN(s.comment_count)) /
                    MAX((julianday(MAX(s.recorded_at))-julianday(MIN(s.recorded_at)))*24.0,0.25)
                  ELSE NULL END observed_velocity
           FROM articles a LEFT JOIN article_snapshots s ON s.article_id=a.id
             AND s.recorded_at>=datetime({snapshot_as_of}, ?)
           WHERE a.posted_at>=datetime({as_of}, ?)
           GROUP BY a.id ORDER BY a.score DESC""".format(
            as_of=_public_as_of_sql(),
            snapshot_as_of=_public_snapshot_as_of_sql(),
        ),
        [f"-{hours} hours", f"-{hours} hours"],
    )
    if df.empty:
        return pd.DataFrame(columns=["id", "title", "url", "category", "score", "comment_count", "posted_at",
                                     "age_hours", "snapshot_count", "observed_velocity", "momentum_score",
                                     "momentum_basis", "confidence"])
    result = df.copy()
    age = pd.to_numeric(result["age_hours"], errors="coerce").fillna(0).clip(lower=0.25)
    reactions = pd.to_numeric(result["score"], errors="coerce").fillna(0) + pd.to_numeric(
        result["comment_count"], errors="coerce"
    ).fillna(0) * 1.5
    early_velocity = reactions / age
    observed = pd.to_numeric(result["observed_velocity"], errors="coerce")
    result["raw_momentum"] = observed.fillna(early_velocity)
    max_momentum = float(result["raw_momentum"].max())
    result["momentum_score"] = (
        result["raw_momentum"] / max_momentum * 100 if max_momentum > 0 else result["raw_momentum"] * 0
    ).round(1)
    result["momentum_basis"] = result["snapshot_count"].map(
        lambda count: "実測増加速度" if int(count or 0) >= 2 else "投稿後の反応速度"
    )
    result["confidence"] = result["snapshot_count"].map(
        lambda count: "高" if int(count or 0) >= 3 else ("中" if int(count or 0) >= 2 else "低")
    )
    return result.sort_values(["momentum_score", "score"], ascending=False).head(limit)


def get_keyword_trends(days: int = 7) -> pd.DataFrame:
    return fetch_df(
        """SELECT k.id keyword_id, k.word, k.first_seen_at, kds.stat_date, kds.appearance_count,
                  kds.novelty_score,
                  AVG(kds.appearance_count) OVER (PARTITION BY k.id) period_average
           FROM keyword_daily_stats kds JOIN keywords k ON k.id=kds.keyword_id
           WHERE kds.stat_date >= date({as_of}, ?) ORDER BY kds.stat_date""".format(
            as_of=_public_stat_date_sql("keyword_daily_stats")
        ),
        [f"-{days} days"],
    )


def get_keyword_category_trends(days: int = 7, keyword_id=None) -> pd.DataFrame:
    sql = """SELECT k.word, s.stat_date, s.category, s.appearance_count
             FROM keyword_category_daily_stats s JOIN keywords k ON k.id=s.keyword_id
             WHERE s.stat_date >= date({as_of}, ?)""".format(
        as_of=_public_stat_date_sql("keyword_category_daily_stats")
    )
    params = [f"-{days} days"]
    if keyword_id:
        sql += " AND s.keyword_id=?"
        params.append(keyword_id)
    return fetch_df(sql + " ORDER BY s.stat_date", params)


def get_category_momentum(hours: int = 24) -> pd.DataFrame:
    df = fetch_df(
        """SELECT COALESCE(category,'Other') category,
                  SUM(CASE WHEN posted_at>=datetime({as_of}, ?) THEN 1 ELSE 0 END) current_count,
                  SUM(CASE WHEN posted_at<datetime({as_of}, ?) AND posted_at>=datetime({as_of}, ?) THEN 1 ELSE 0 END) previous_count,
                  AVG(CASE WHEN posted_at>=datetime({as_of}, ?) THEN score+comment_count END) current_engagement,
                  AVG(CASE WHEN posted_at<datetime({as_of}, ?) AND posted_at>=datetime({as_of}, ?)
                      THEN score+comment_count END) previous_engagement
           FROM articles WHERE posted_at>=datetime({as_of}, ?) GROUP BY COALESCE(category,'Other')""".format(
            as_of=_public_as_of_sql()
        ),
        [
            f"-{hours} hours", f"-{hours} hours", f"-{hours * 2} hours",
            f"-{hours} hours", f"-{hours} hours", f"-{hours * 2} hours",
            f"-{hours * 2} hours",
        ],
    )
    if df.empty:
        return pd.DataFrame(columns=["category", "current_count", "previous_count", "current_engagement",
                                     "previous_engagement", "volume_change", "engagement_change", "momentum"])
    result = df.copy()
    current = pd.to_numeric(result["current_count"], errors="coerce").fillna(0)
    previous = pd.to_numeric(result["previous_count"], errors="coerce").fillna(0)
    current_engagement = pd.to_numeric(result["current_engagement"], errors="coerce").fillna(0)
    previous_engagement = pd.to_numeric(result["previous_engagement"], errors="coerce").fillna(0)
    result["volume_change"] = ((current + 1) / (previous + 1) - 1) * 100
    result["engagement_change"] = ((current_engagement + 1) / (previous_engagement + 1) - 1) * 100
    result["momentum"] = (result["volume_change"] * 0.6 + result["engagement_change"].clip(-300, 300) * 0.4).round(1)
    return result.sort_values("momentum", ascending=False)


def get_keyword_shifts(window_days: int = 3, limit: int = 40) -> pd.DataFrame:
    df = fetch_df(
        """SELECT k.word,k.first_seen_at,
                  SUM(CASE WHEN kds.stat_date>=date({as_of}, ?) THEN kds.appearance_count ELSE 0 END) current_count,
                  SUM(CASE WHEN kds.stat_date<date({as_of}, ?)
                            AND kds.stat_date>=date({as_of}, ?) THEN kds.appearance_count ELSE 0 END) previous_count,
                  MAX(CASE WHEN kds.stat_date>=date({as_of}, ?) THEN kds.novelty_score ELSE 0 END) novelty_score
           FROM keyword_daily_stats kds JOIN keywords k ON k.id=kds.keyword_id
           WHERE kds.stat_date>=date({as_of}, ?)
             AND LOWER(k.word) NOT IN
               ('all','will','run','pdf','also','one','two','get','being','does','could','would','should',
                'these','those','very','much','many','open','article','source','now','show','ask','news')
           GROUP BY k.id,k.word,k.first_seen_at
           HAVING current_count+previous_count>=2""".format(
            as_of=_public_stat_date_sql("keyword_daily_stats")
        ),
        [
            f"-{window_days} days", f"-{window_days} days", f"-{window_days * 2} days",
            f"-{window_days} days", f"-{window_days * 2} days",
        ],
    )
    if df.empty:
        return pd.DataFrame(columns=["word", "first_seen_at", "current_count", "previous_count", "novelty_score",
                                     "change_percent", "shift_score", "direction"])
    result = df.copy()
    current = pd.to_numeric(result["current_count"], errors="coerce").fillna(0)
    previous = pd.to_numeric(result["previous_count"], errors="coerce").fillna(0)
    result["change_percent"] = ((current + 1) / (previous + 1) - 1) * 100
    result["shift_score"] = (result["change_percent"].abs() * (current + previous).map(math.log1p)).round(1)
    result["direction"] = result["change_percent"].map(
        lambda value: "急上昇" if value >= 50 else ("減速" if value <= -40 else "安定")
    )
    return result.sort_values("shift_score", ascending=False).head(limit)


def get_article_comments(article_id: int, limit: int = 100) -> pd.DataFrame:
    return fetch_df(
        """SELECT hn_comment_id,parent_hn_id,author,text,depth,display_order,posted_at,fetched_at
           FROM article_comments WHERE article_id=? ORDER BY display_order LIMIT ?""",
        [article_id, limit],
    )


def get_article_keywords(article_id: int) -> pd.DataFrame:
    return fetch_df(
        """SELECT k.id,k.word,ak.relevance_score,ak.rank_position
           FROM article_keywords ak JOIN keywords k ON k.id=ak.keyword_id
           WHERE ak.article_id=? ORDER BY ak.rank_position,k.word""",
        [article_id],
    )


def get_article_snapshots(article_id: int) -> pd.DataFrame:
    return fetch_df("SELECT recorded_at, score, comment_count FROM article_snapshots WHERE article_id=? ORDER BY recorded_at", [article_id])


def _tokens(title: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.-]{3,}", (title or "").lower())) - DOMAIN_GENERIC_TERMS


def get_public_related_articles(article_id: int, days: int = 3650, limit: int = 8) -> pd.DataFrame:
    article = get_article_by_id(article_id)
    if not article:
        return pd.DataFrame()
    candidates = get_public_articles(days, limit=300)
    candidates = candidates[candidates["id"] != article_id].copy()
    if candidates.empty:
        return candidates
    source_tokens = _tokens(article.get("title", ""))
    keyword_ids = set(get_article_keywords(article_id).get("id", []))
    keyword_map = {}
    if keyword_ids:
        links = fetch_df(
            f"SELECT article_id, keyword_id FROM article_keywords WHERE keyword_id IN ({','.join(['?'] * len(keyword_ids))})",
            list(keyword_ids),
        )
        keyword_map = links.groupby("article_id")["keyword_id"].apply(set).to_dict() if not links.empty else {}
    scores, reasons = [], []
    for _, row in candidates.iterrows():
        shared = len(keyword_map.get(row["id"], set()) & keyword_ids)
        same_category = bool(article.get("category") and row.get("category") == article.get("category"))
        same_author = bool(article.get("author") and row.get("author") == article.get("author"))
        target_tokens = _tokens(row.get("title", ""))
        union = source_tokens | target_tokens
        jaccard = len(source_tokens & target_tokens) / len(union) if union else 0
        score = shared * 20 + same_category * 12 + same_author * 8 + jaccard * 25 + float(row["importance_score"]) * 0.2
        reason = []
        if shared:
            reason.append(f"{shared} shared keywords")
        if same_category:
            reason.append("same category")
        if same_author:
            reason.append("same submitter")
        if jaccard >= 0.2:
            reason.append("similar title")
        scores.append(score)
        reasons.append(" / ".join(reason) or "similar attention score")
    candidates["recommendation_score"] = scores
    candidates["recommendation_reason"] = reasons
    return candidates.sort_values("recommendation_score", ascending=False).head(limit)
