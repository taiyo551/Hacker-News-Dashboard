import os
import time
from datetime import datetime, timezone

import mysql.connector
import requests
from dotenv import load_dotenv

from batch_run import run_recorded

load_dotenv()
BASE = "https://hacker-news.firebaseio.com/v0"
TOP_N = 500
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"), "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "hn_dashboard"),
}


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def unix_to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None


def infer_category(title, item_type):
    text = (title or "").lower()
    rules = [
        ("AI", ["llm", " ai ", "machine learning", "gpt", "deepseek", "claude", "model", "computer vision", "agentic", "agents"]),
        ("Security", ["security", "vuln", "cve", "exploit", "xss", "rce", "certificate", "encrypt", "malware", "privacy"]),
        ("Programming", ["python", "rust", "golang", "compiler", "programming", "debug", "type-check", "npm", "git ", "software"]),
        ("Web", ["web", "browser", "frontend", "backend", "react", "javascript", "internet", "http"]),
        ("OS", ["linux", "kernel", "windows", "macos", "bsd"]),
        ("Startup", ["startup", "y combinator", "funding", "seed"]),
        ("Database", ["mysql", "postgres", "sqlite", "database", "sql"]),
        ("DevOps", ["docker", "kubernetes", "terraform", "ci/cd", "cloud"]),
        ("Hardware", ["hardware", "iphone", "apple", "chip", "cpu", "gpu", "coreboot", "thinkpad"]),
        ("Science", ["science", "star", "solar", "physics", "biology", "molecule", "plants", "space"]),
        ("Policy", ["fcc", "eu ", "government", "regulation", "sanction", "telecom", "ban "]),
    ]
    for category, words in rules:
        if any(word in f" {text} " for word in words):
            return category
    return "Job" if item_type == "job" else "Other"


def fetch_item(session, item_id):
    try:
        response = session.get(f"{BASE}/item/{item_id}.json", timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def upsert_article(cur, item):
    cur.execute(
        """INSERT INTO articles(hn_id,title,url,author,type,category,score,comment_count,posted_at,fetched_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE title=VALUES(title),url=VALUES(url),author=VALUES(author),
           type=VALUES(type),category=VALUES(category),score=VALUES(score),
           comment_count=VALUES(comment_count),posted_at=VALUES(posted_at),fetched_at=VALUES(fetched_at)""",
        [item.get("id"), item.get("title") or "", item.get("url"), item.get("by"),
         item.get("type") or "story", infer_category(item.get("title"), item.get("type")),
         int(item.get("score") or 0), int(item.get("descendants") or 0), unix_to_dt(item.get("time")),
         datetime.now(timezone.utc).replace(tzinfo=None)],
    )


def _run():
    conn = get_conn()
    cur = conn.cursor()
    with requests.Session() as session:
        response = session.get(f"{BASE}/topstories.json", timeout=30)
        response.raise_for_status()
        top_ids = response.json()[:TOP_N]
        cur.execute("SELECT hn_id FROM articles WHERE posted_at>=NOW()-INTERVAL 48 HOUR")
        target_ids = list(dict.fromkeys(top_ids + [row[0] for row in cur.fetchall()]))
        success = failure = 0
        for index, item_id in enumerate(target_ids, 1):
            item = fetch_item(session, item_id)
            if item:
                upsert_article(cur, item)
                success += 1
            else:
                failure += 1
            if index % 25 == 0:
                conn.commit()
            time.sleep(0.05)
    conn.commit()
    cur.close()
    conn.close()
    return {"processed": len(target_ids), "success": success, "failure": failure}


def main():
    return run_recorded("fetch_articles", get_conn, _run)


if __name__ == "__main__":
    main()
