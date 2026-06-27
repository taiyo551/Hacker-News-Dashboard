import html
import os
from datetime import datetime, timezone

import mysql.connector
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from batch_run import run_recorded

load_dotenv()
BASE = "https://hacker-news.firebaseio.com/v0"
MAX_COMMENTS = 100
ARTICLES_PER_RUN = 20
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASSWORD"), "database": os.getenv("DB_NAME"),
}


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def clean_comment(value):
    return " ".join(BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True).split())


def collect_comments(session, root_item, limit=MAX_COMMENTS):
    output = []

    def visit(comment_id, depth):
        if len(output) >= limit:
            return
        try:
            item = session.get(f"{BASE}/item/{comment_id}.json", timeout=10).json() or {}
        except requests.RequestException:
            return
        if not item.get("deleted") and not item.get("dead"):
            text = clean_comment(item.get("text"))
            if text:
                output.append({
                    "hn_comment_id": item.get("id"), "parent_hn_id": item.get("parent"),
                    "author": item.get("by"), "text": text, "depth": depth,
                    "posted_at": datetime.fromtimestamp(item["time"], tz=timezone.utc).replace(tzinfo=None)
                    if item.get("time") else None,
                })
        for child_id in item.get("kids") or []:
            visit(child_id, depth + 1)

    for child_id in root_item.get("kids") or []:
        visit(child_id, 0)
    return output[:limit]


def _run():
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT a.id,a.hn_id,comment_state.last_fetched_at
           FROM articles a
           LEFT JOIN (SELECT article_id,MAX(fetched_at) last_fetched_at FROM article_comments GROUP BY article_id)
             comment_state ON comment_state.article_id=a.id
           WHERE a.posted_at>=NOW()-INTERVAL 2 DAY AND a.comment_count>0
             AND (comment_state.last_fetched_at IS NULL OR comment_state.last_fetched_at<NOW()-INTERVAL 1 HOUR)
           ORDER BY comment_state.last_fetched_at IS NULL DESC,a.comment_count DESC LIMIT %s""",
        [ARTICLES_PER_RUN],
    )
    articles = cur.fetchall()
    success = failure = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with requests.Session() as session:
        for article in articles:
            try:
                root = session.get(f"{BASE}/item/{article['hn_id']}.json", timeout=10).json() or {}
                comments = collect_comments(session, root)
                cur.execute("DELETE FROM article_comments WHERE article_id=%s", [article["id"]])
                for order, comment in enumerate(comments):
                    cur.execute(
                        """INSERT INTO article_comments(article_id,hn_comment_id,parent_hn_id,author,text,depth,
                           display_order,posted_at,fetched_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [article["id"], comment["hn_comment_id"], comment["parent_hn_id"], comment["author"],
                         comment["text"], comment["depth"], order, comment["posted_at"], now],
                    )
                conn.commit()
                success += 1
            except (requests.RequestException, mysql.connector.Error):
                conn.rollback()
                failure += 1
    cur.close()
    conn.close()
    return {"processed": len(articles), "success": success, "failure": failure}


def main():
    return run_recorded("fetch_comments", get_conn, _run)


if __name__ == "__main__":
    main()
