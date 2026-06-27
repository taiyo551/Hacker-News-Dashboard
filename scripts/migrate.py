from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from db import get_connection


def main():
    sql_path = Path(__file__).with_name("migrate.sql")
    statements = [statement.strip() for statement in sql_path.read_text(encoding="utf-8").split(";") if statement.strip()]
    conn = get_connection()
    try:
        cur = conn.cursor()
        for statement in statements:
            cur.execute(statement)
        article_keyword_columns = {
            "relevance_score": "DECIMAL(8,6) NOT NULL DEFAULT 0",
            "rank_position": "INT NOT NULL DEFAULT 999",
        }
        for name, definition in article_keyword_columns.items():
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='article_keywords' AND COLUMN_NAME=%s""",
                [name],
            )
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE article_keywords ADD COLUMN {name} {definition}")
        conn.commit()
    finally:
        conn.close()
    print(f"Applied {len(statements)} table migrations and verified mutable columns.")


if __name__ == "__main__":
    main()
