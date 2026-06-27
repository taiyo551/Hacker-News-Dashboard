import os
from datetime import datetime, timezone

import mysql.connector
import requests
from dotenv import load_dotenv

from batch_run import run_recorded

load_dotenv()
BASE = "https://hacker-news.firebaseio.com/v0"
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASSWORD"), "database": os.getenv("DB_NAME"),
}


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def _run():
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id,hn_id FROM articles WHERE posted_at>=NOW()-INTERVAL 1 DAY")
    articles = cur.fetchall()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    success = failure = 0
    with requests.Session() as session:
        for article in articles:
            try:
                response = session.get(f"{BASE}/item/{article['hn_id']}.json", timeout=15)
                response.raise_for_status()
                item = response.json() or {}
                score, comments = int(item.get("score") or 0), int(item.get("descendants") or 0)
                cur.execute("INSERT INTO article_snapshots(article_id,score,comment_count,recorded_at) VALUES(%s,%s,%s,%s)",
                            [article["id"], score, comments, now])
                cur.execute("UPDATE articles SET score=%s,comment_count=%s,fetched_at=%s WHERE id=%s",
                            [score, comments, now, article["id"]])
                success += 1
            except requests.RequestException:
                failure += 1
    conn.commit()
    cur.close()
    conn.close()
    return {"processed": len(articles), "success": success, "failure": failure}


def main():
    return run_recorded("record_snapshots", get_conn, _run)


if __name__ == "__main__":
    main()
