from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch"))

from db import get_connection


def main():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE keyword_daily_stats")
        cur.execute(
            """INSERT INTO keyword_daily_stats(keyword_id,stat_date,appearance_count,novelty_score)
               SELECT ak.keyword_id,DATE(a.posted_at),COUNT(*),0
               FROM article_keywords ak JOIN articles a ON a.id=ak.article_id
               WHERE a.posted_at IS NOT NULL
               GROUP BY ak.keyword_id,DATE(a.posted_at)"""
        )
        cur.execute(
            """UPDATE keyword_daily_stats current_stats LEFT JOIN (
                 SELECT keyword_id,stat_date,AVG(previous_count) average_count
                 FROM (
                   SELECT current_row.keyword_id,current_row.stat_date,previous_row.appearance_count previous_count
                   FROM keyword_daily_stats current_row
                   LEFT JOIN keyword_daily_stats previous_row ON previous_row.keyword_id=current_row.keyword_id
                     AND previous_row.stat_date<current_row.stat_date
                 ) history GROUP BY keyword_id,stat_date
               ) past ON past.keyword_id=current_stats.keyword_id AND past.stat_date=current_stats.stat_date
               SET current_stats.novelty_score=CASE
                 WHEN past.average_count IS NULL OR past.average_count=0 THEN current_stats.appearance_count
                 ELSE current_stats.appearance_count/past.average_count END"""
        )
        cur.execute("TRUNCATE TABLE keyword_category_daily_stats")
        cur.execute(
            """INSERT INTO keyword_category_daily_stats(keyword_id,stat_date,category,appearance_count)
               SELECT ak.keyword_id,DATE(a.posted_at),COALESCE(a.category,'Other'),COUNT(*)
               FROM article_keywords ak JOIN articles a ON a.id=ak.article_id
               GROUP BY ak.keyword_id,DATE(a.posted_at),COALESCE(a.category,'Other')"""
        )
        conn.commit()
    finally:
        conn.close()
    print("Analytics backfill completed.")


if __name__ == "__main__":
    main()
