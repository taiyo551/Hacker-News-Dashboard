from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch"))

from db import get_connection
from fetch_articles import infer_category


def main():
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id,title,type,category FROM articles")
        articles = cur.fetchall()
        changed = 0
        for article in articles:
            category = infer_category(article["title"], article["type"])
            if category != article["category"]:
                cur.execute("UPDATE articles SET category=%s WHERE id=%s", [category, article["id"]])
                changed += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Reclassified {changed} of {len(articles)} articles.")


if __name__ == "__main__":
    main()
