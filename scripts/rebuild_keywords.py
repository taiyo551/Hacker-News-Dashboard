from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch"))

from db import get_connection
from extract_keywords import main as extract_keywords


def main():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM keyword_category_daily_stats")
        cur.execute("DELETE FROM keyword_daily_stats")
        cur.execute("DELETE FROM article_keywords")
        cur.execute("DELETE FROM keywords")
        conn.commit()
    finally:
        conn.close()
    extract_keywords(full_rebuild=True)
    print("Keyword analytics rebuilt with the current extraction rules.")


if __name__ == "__main__":
    main()
