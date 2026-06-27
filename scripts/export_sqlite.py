from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from db import ACTIVE_BATCH_JOBS, get_connection  # noqa: E402


PUBLIC_TABLES = [
    "articles",
    "article_content",
    "article_keywords",
    "keywords",
    "article_comments",
    "article_snapshots",
    "keyword_daily_stats",
    "keyword_category_daily_stats",
    "batch_runs",
]


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_type(mysql_type: str) -> str:
    mysql_type = mysql_type.lower()
    if mysql_type in {"tinyint", "smallint", "mediumint", "int", "bigint"}:
        return "INTEGER"
    if mysql_type in {"decimal", "float", "double", "real"}:
        return "REAL"
    if mysql_type in {"date", "datetime", "timestamp", "time"}:
        return "TEXT"
    return "TEXT"


def sqlite_value(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


def table_columns(mysql_conn, table: str) -> list[tuple[str, str]]:
    cur = mysql_conn.cursor(dictionary=True)
    cur.execute(
        """SELECT COLUMN_NAME, DATA_TYPE
           FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s
           ORDER BY ORDINAL_POSITION""",
        [table],
    )
    return [(row["COLUMN_NAME"], sqlite_type(row["DATA_TYPE"])) for row in cur.fetchall()]


def create_table(sqlite_conn, mysql_conn, table: str):
    columns = table_columns(mysql_conn, table)
    if not columns:
        raise RuntimeError(f"MySQL table not found or has no columns: {table}")
    definitions = ", ".join(f"{quote_identifier(name)} {column_type}" for name, column_type in columns)
    sqlite_conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
    sqlite_conn.execute(f"CREATE TABLE {quote_identifier(table)} ({definitions})")
    return [name for name, _ in columns]


def copy_query(mysql_conn, sqlite_conn, table: str, query: str, params=None, chunk_size: int = 1000) -> int:
    columns = create_table(sqlite_conn, mysql_conn, table)
    mysql_cur = mysql_conn.cursor(dictionary=True)
    mysql_cur.execute(query, params or [])
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    insert_sql = f"INSERT INTO {quote_identifier(table)} ({column_sql}) VALUES ({placeholders})"
    copied = 0
    while True:
        rows = mysql_cur.fetchmany(chunk_size)
        if not rows:
            break
        values = [[sqlite_value(row.get(column)) for column in columns] for row in rows]
        sqlite_conn.executemany(insert_sql, values)
        copied += len(rows)
    return copied


def create_indexes(sqlite_conn):
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_articles_posted_at ON articles(posted_at)",
        "CREATE INDEX IF NOT EXISTS idx_articles_category_type ON articles(category,type)",
        "CREATE INDEX IF NOT EXISTS idx_article_content_article ON article_content(article_id)",
        "CREATE INDEX IF NOT EXISTS idx_article_keywords_article_rank ON article_keywords(article_id,rank_position)",
        "CREATE INDEX IF NOT EXISTS idx_article_keywords_keyword ON article_keywords(keyword_id)",
        "CREATE INDEX IF NOT EXISTS idx_article_comments_article_order ON article_comments(article_id,display_order)",
        "CREATE INDEX IF NOT EXISTS idx_article_snapshots_article_recorded ON article_snapshots(article_id,recorded_at)",
        "CREATE INDEX IF NOT EXISTS idx_keyword_daily_stats_date ON keyword_daily_stats(stat_date,keyword_id)",
        "CREATE INDEX IF NOT EXISTS idx_keyword_category_daily_stats_date ON keyword_category_daily_stats(stat_date,keyword_id,category)",
        "CREATE INDEX IF NOT EXISTS idx_batch_runs_job_id ON batch_runs(job_name,id)",
    ]
    for statement in statements:
        sqlite_conn.execute(statement)


def export(days: int, output: Path, batch_runs: int):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_suffix(output.suffix + ".tmp")
    if temp_output.exists():
        temp_output.unlink()

    mysql_conn = get_connection()
    sqlite_conn = sqlite3.connect(temp_output)
    try:
        sqlite_conn.execute("PRAGMA journal_mode=OFF")
        sqlite_conn.execute("PRAGMA synchronous=OFF")
        sqlite_conn.execute("PRAGMA foreign_keys=OFF")
        sqlite_conn.execute("BEGIN")

        article_window = "a.posted_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        stats_window = "stat_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"

        counts = {
            "articles": copy_query(
                mysql_conn,
                sqlite_conn,
                "articles",
                f"SELECT * FROM articles a WHERE {article_window}",
                [days],
            ),
            "article_content": copy_query(
                mysql_conn,
                sqlite_conn,
                "article_content",
                f"""SELECT ac.* FROM article_content ac
                    JOIN articles a ON a.id=ac.article_id WHERE {article_window}""",
                [days],
            ),
            "article_keywords": copy_query(
                mysql_conn,
                sqlite_conn,
                "article_keywords",
                f"""SELECT ak.* FROM article_keywords ak
                    JOIN articles a ON a.id=ak.article_id WHERE {article_window}""",
                [days],
            ),
            "article_comments": copy_query(
                mysql_conn,
                sqlite_conn,
                "article_comments",
                f"""SELECT c.* FROM article_comments c
                    JOIN articles a ON a.id=c.article_id WHERE {article_window}""",
                [days],
            ),
            "article_snapshots": copy_query(
                mysql_conn,
                sqlite_conn,
                "article_snapshots",
                f"""SELECT s.* FROM article_snapshots s
                    JOIN articles a ON a.id=s.article_id
                    WHERE {article_window} AND s.recorded_at >= DATE_SUB(NOW(), INTERVAL %s DAY)""",
                [days, days],
            ),
            "keyword_daily_stats": copy_query(
                mysql_conn,
                sqlite_conn,
                "keyword_daily_stats",
                f"SELECT * FROM keyword_daily_stats WHERE {stats_window}",
                [days],
            ),
            "keyword_category_daily_stats": copy_query(
                mysql_conn,
                sqlite_conn,
                "keyword_category_daily_stats",
                f"SELECT * FROM keyword_category_daily_stats WHERE {stats_window}",
                [days],
            ),
            "keywords": copy_query(
                mysql_conn,
                sqlite_conn,
                "keywords",
                f"""SELECT DISTINCT k.* FROM keywords k
                    WHERE EXISTS (
                      SELECT 1 FROM article_keywords ak
                      JOIN articles a ON a.id=ak.article_id
                      WHERE ak.keyword_id=k.id AND {article_window}
                    )
                    OR EXISTS (
                      SELECT 1 FROM keyword_daily_stats kds
                      WHERE kds.keyword_id=k.id AND kds.stat_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    )
                    OR EXISTS (
                      SELECT 1 FROM keyword_category_daily_stats kcds
                      WHERE kcds.keyword_id=k.id AND kcds.stat_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    )""",
                [days, days, days],
            ),
            "batch_runs": copy_query(
                mysql_conn,
                sqlite_conn,
                "batch_runs",
                f"""SELECT * FROM batch_runs
                    WHERE job_name IN ({','.join(['%s'] * len(ACTIVE_BATCH_JOBS))})
                    ORDER BY id DESC LIMIT %s""",
                [*ACTIVE_BATCH_JOBS, batch_runs],
            ),
        }
        create_indexes(sqlite_conn)
        sqlite_conn.commit()
    except Exception:
        sqlite_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        mysql_conn.close()

    temp_output.replace(output)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Export public dashboard data from MySQL to SQLite.")
    parser.add_argument("--days", type=int, default=30, help="Number of recent days to include.")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "hn_dashboard.sqlite")
    parser.add_argument("--batch-runs", type=int, default=200, help="Number of recent batch run rows to include.")
    args = parser.parse_args()

    counts = export(args.days, args.output, args.batch_runs)
    size_mb = args.output.stat().st_size / 1024 / 1024
    print(f"Exported SQLite database: {args.output}")
    print(f"Size: {size_mb:.2f} MiB")
    for table in PUBLIC_TABLES:
        print(f"{table}: {counts.get(table, 0)} rows")


if __name__ == "__main__":
    main()
