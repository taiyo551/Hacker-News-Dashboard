import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

from batch_run import run_recorded

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from analysis import DOMAIN_GENERIC_TERMS, STANDARD_MAX_DF, STANDARD_MIN_DF, extract_weighted_keywords

load_dotenv()
DB_CONFIG = {k: v for k, v in {
    "host": os.getenv("DB_HOST"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
}.items() if v is not None}

ALIASES = {"javascript": "js", "typescript": "ts", "postgresql": "postgres", "k8s": "kubernetes"}
FALLBACK_STOPWORDS = {"the", "and", "for", "with", "using", "from", "this", "that", "new"} | DOMAIN_GENERIC_TERMS


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def extract_keywords(text):
    """Compatibility helper; persisted extraction uses corpus-level TF-IDF in _run."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", text or "")
    return sorted({
        ALIASES.get(word, word) for raw in words
        if (word := raw.lower().strip(".-")) not in FALLBACK_STOPWORDS
    })


def _run(full_rebuild=False):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    limit_clause = "" if full_rebuild else "LIMIT 3000"
    cur.execute(
        f"""SELECT a.id,a.title,COALESCE(a.category,'Other') category,DATE(a.posted_at) stat_date,
                  COALESCE(ac.snippet,'') snippet,
                  COALESCE((SELECT GROUP_CONCAT(c.text ORDER BY c.display_order SEPARATOR ' ')
                            FROM article_comments c WHERE c.article_id=a.id AND c.display_order<10),'') comments,
                  EXISTS(SELECT 1 FROM article_keywords existing WHERE existing.article_id=a.id) is_processed
           FROM articles a LEFT JOIN article_content ac ON ac.article_id=a.id
           WHERE a.title IS NOT NULL ORDER BY a.fetched_at DESC {limit_clause}"""
    )
    articles = cur.fetchall()
    pending = list(range(len(articles))) if full_rebuild else [
        index for index, article in enumerate(articles) if not article["is_processed"]
    ]
    if not pending:
        cur.close()
        conn.close()
        return {"processed": 0, "success": 0, "failure": 0}
    try:
        extracted = extract_weighted_keywords(
            [article["title"] for article in articles],
            [article["snippet"] for article in articles],
            [article["comments"] for article in articles],
            max_df=STANDARD_MAX_DF,
            min_df=STANDARD_MIN_DF,
        )
    except ValueError:
        cur.close()
        conn.close()
        return {"processed": len(pending), "success": 0, "failure": len(pending), "error": "有効なTF-IDF語彙がありません"}
    for index in pending:
        article = articles[index]
        stat_date = article["stat_date"] or datetime.now(timezone.utc).date()
        for selected in extracted[index]:
            raw_word = selected["word"]
            word = ALIASES.get(raw_word, raw_word)
            cur.execute("""INSERT INTO keywords(word,first_seen_at,total_appearances) VALUES(%s,NOW(),1)
                           ON DUPLICATE KEY UPDATE total_appearances=total_appearances+1""", [word])
            cur.execute("SELECT id FROM keywords WHERE word=%s", [word])
            keyword_id = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO article_keywords(article_id,keyword_id,relevance_score,rank_position)
                   VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                   relevance_score=VALUES(relevance_score),rank_position=VALUES(rank_position)""",
                [article["id"], keyword_id, selected["relevance_score"], selected["rank_position"]],
            )
            cur.execute("""INSERT INTO keyword_daily_stats(keyword_id,stat_date,appearance_count,novelty_score)
                           VALUES(%s,%s,1,0) ON DUPLICATE KEY UPDATE appearance_count=appearance_count+1""", [keyword_id, stat_date])
            cur.execute("""INSERT INTO keyword_category_daily_stats(keyword_id,stat_date,category,appearance_count)
                           VALUES(%s,%s,%s,1) ON DUPLICATE KEY UPDATE appearance_count=appearance_count+1""",
                        [keyword_id, stat_date, article["category"]])
    cur.execute("""UPDATE keyword_daily_stats kds LEFT JOIN (
                     SELECT keyword_id,AVG(appearance_count) avg_count FROM keyword_daily_stats
                     WHERE stat_date<CURDATE() GROUP BY keyword_id) past ON past.keyword_id=kds.keyword_id
                   SET kds.novelty_score=CASE WHEN past.avg_count IS NULL OR past.avg_count=0
                     THEN kds.appearance_count ELSE kds.appearance_count/past.avg_count END
                   WHERE kds.stat_date>=CURDATE()-INTERVAL 30 DAY""")
    conn.commit()
    cur.close()
    conn.close()
    return {"processed": len(pending), "success": len(pending), "failure": 0}


def main(full_rebuild=False):
    return run_recorded("extract_keywords", get_conn, lambda: _run(full_rebuild))


if __name__ == "__main__":
    main()
