import os

import mysql.connector
import requests
from dotenv import load_dotenv

from batch_run import run_recorded

load_dotenv()
OLLAMA_URL = os.getenv("OLLAMA_URL", "").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:4b")
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASSWORD"), "database": os.getenv("DB_NAME"),
}


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def _run():
    if not OLLAMA_URL:
        return {"processed": 0, "success": 0, "failure": 0, "error": "OLLAMA_URL未設定のためスキップ"}
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id,title,url FROM articles WHERE is_summarized=0 ORDER BY score DESC LIMIT 20")
    articles = cur.fetchall()
    success = failure = 0
    for article in articles:
        prompt = f"次のHacker News記事タイトルを日本語で2文以内に要約してください。\n{article['title']}\n{article.get('url') or ''}"
        try:
            response = requests.post(f"{OLLAMA_URL}/api/generate",
                                     json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=90)
            response.raise_for_status()
            summary = response.json().get("response", "").strip()
            if not summary:
                raise ValueError("empty summary")
            cur.execute("UPDATE articles SET summary_ja=%s,is_summarized=1 WHERE id=%s", [summary, article["id"]])
            success += 1
        except (requests.RequestException, ValueError):
            failure += 1
    conn.commit()
    cur.close()
    conn.close()
    return {"processed": len(articles), "success": success, "failure": failure}


def main():
    return run_recorded("summarize_articles", get_conn, _run)


if __name__ == "__main__":
    main()
