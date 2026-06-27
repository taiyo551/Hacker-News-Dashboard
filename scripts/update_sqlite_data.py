from __future__ import annotations

import argparse
import html
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from analysis import STANDARD_MAX_DF, STANDARD_MIN_DF, extract_weighted_keywords  # noqa: E402

BASE = "https://hacker-news.firebaseio.com/v0"
ACTIVE_BATCH_JOBS = (
    "fetch_articles",
    "record_snapshots",
    "extract_keywords",
    "fetch_comments",
)
ALIASES = {"javascript": "js", "typescript": "ts", "postgresql": "postgres", "k8s": "kubernetes"}


class IdAllocator:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.next_values: dict[str, int] = {}

    def next_id(self, table: str) -> int:
        if table not in self.next_values:
            current = self.conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            self.next_values[table] = int(current or 0) + 1
        value = self.next_values[table]
        self.next_values[table] += 1
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sql_time(value: datetime | None = None) -> str:
    return (value or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def unix_to_sql_time(value) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value, limit=2000) -> str:
    text = BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:limit]


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


def fetch_json(session: requests.Session, url: str, timeout=20):
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def record_batch(conn: sqlite3.Connection, ids: IdAllocator, job_name: str, worker):
    started = sql_time()
    run_id = ids.next_id("batch_runs")
    conn.execute(
        """INSERT INTO batch_runs(id,job_name,started_at,status,processed_count,success_count,failure_count)
           VALUES(?,?,?,?,0,0,0)""",
        [run_id, job_name, started, "running"],
    )
    conn.commit()
    status = "failed"
    processed = success = failure = 0
    error = None
    try:
        result = worker() or {}
        processed = int(result.get("processed", 0))
        success = int(result.get("success", processed))
        failure = int(result.get("failure", max(processed - success, 0)))
        status = "partial" if failure and success else ("failed" if failure else "success")
        error = result.get("error")
        return result
    except Exception as exc:
        failure = max(failure, 1)
        error = repr(exc)
        raise
    finally:
        conn.execute(
            """UPDATE batch_runs SET finished_at=?,status=?,processed_count=?,success_count=?,
               failure_count=?,error_message=? WHERE id=?""",
            [sql_time(), status, processed, success, failure, error, run_id],
        )
        conn.commit()


def upsert_article(conn: sqlite3.Connection, ids: IdAllocator, item: dict, fetched_at: str) -> bool:
    if not item or not item.get("id") or not item.get("title"):
        return False
    existing = conn.execute("SELECT id FROM articles WHERE hn_id=? ORDER BY id LIMIT 1", [item["id"]]).fetchone()
    values = [
        item.get("title") or "",
        item.get("url"),
        item.get("by"),
        item.get("type") or "story",
        infer_category(item.get("title"), item.get("type")),
        int(item.get("score") or 0),
        int(item.get("descendants") or 0),
        unix_to_sql_time(item.get("time")),
        fetched_at,
    ]
    if existing:
        conn.execute(
            """UPDATE articles SET title=?,url=?,author=?,type=?,category=?,score=?,
               comment_count=?,posted_at=?,fetched_at=? WHERE id=?""",
            [*values, existing["id"]],
        )
    else:
        conn.execute(
            """INSERT INTO articles(id,hn_id,title,url,author,type,category,score,comment_count,
               summary_ja,is_summarized,posted_at,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,NULL,0,?,?)""",
            [ids.next_id("articles"), item["id"], *values],
        )

    hn_text = clean_text(item.get("text"), 2000)
    if hn_text:
        article_id = conn.execute("SELECT id FROM articles WHERE hn_id=? ORDER BY id LIMIT 1", [item["id"]]).fetchone()["id"]
        content = conn.execute("SELECT article_id FROM article_content WHERE article_id=?", [article_id]).fetchone()
        if content:
            conn.execute(
                """UPDATE article_content SET hn_text=?,fetch_status=?,fetched_at=?
                   WHERE article_id=? AND (snippet IS NULL OR snippet='')""",
                [hn_text, "success", fetched_at, article_id],
            )
        else:
            conn.execute(
                """INSERT INTO article_content(article_id,snippet,snippet_source,hn_text,fetch_status,fetched_at,error_message)
                   VALUES(?,NULL,NULL,?,'success',?,NULL)""",
                [article_id, hn_text, fetched_at],
            )
    return True


def fetch_articles(conn: sqlite3.Connection, ids: IdAllocator, top_n: int) -> dict:
    fetched_at = sql_time()
    cutoff = sql_time(utc_now() - timedelta(hours=48))
    success = failure = 0
    with requests.Session() as session:
        top_ids = fetch_json(session, f"{BASE}/topstories.json", timeout=30)[:top_n]
        recent_ids = [
            row["hn_id"] for row in conn.execute(
                "SELECT hn_id FROM articles WHERE posted_at>=? AND hn_id IS NOT NULL",
                [cutoff],
            )
        ]
        target_ids = list(dict.fromkeys([*top_ids, *recent_ids]))
        for index, item_id in enumerate(target_ids, 1):
            try:
                item = fetch_json(session, f"{BASE}/item/{item_id}.json", timeout=15) or {}
                success += 1 if upsert_article(conn, ids, item, fetched_at) else 0
            except requests.RequestException:
                failure += 1
            if index % 25 == 0:
                conn.commit()
            time.sleep(0.04)
    conn.commit()
    return {"processed": len(target_ids), "success": success, "failure": failure}


def record_snapshots(conn: sqlite3.Connection, ids: IdAllocator) -> dict:
    cutoff = sql_time(utc_now() - timedelta(days=1))
    articles = list(conn.execute("SELECT id,hn_id FROM articles WHERE posted_at>=? AND hn_id IS NOT NULL", [cutoff]))
    now = sql_time()
    success = failure = 0
    with requests.Session() as session:
        for article in articles:
            try:
                item = fetch_json(session, f"{BASE}/item/{article['hn_id']}.json", timeout=15) or {}
                score = int(item.get("score") or 0)
                comments = int(item.get("descendants") or 0)
                conn.execute(
                    "INSERT INTO article_snapshots(id,article_id,score,comment_count,recorded_at) VALUES(?,?,?,?,?)",
                    [ids.next_id("article_snapshots"), article["id"], score, comments, now],
                )
                conn.execute("UPDATE articles SET score=?,comment_count=?,fetched_at=? WHERE id=?", [score, comments, now, article["id"]])
                success += 1
            except requests.RequestException:
                failure += 1
    conn.commit()
    return {"processed": len(articles), "success": success, "failure": failure}


def collect_comments(session: requests.Session, root_item: dict, limit: int) -> list[dict]:
    output = []

    def visit(comment_id, depth):
        if len(output) >= limit:
            return
        try:
            item = fetch_json(session, f"{BASE}/item/{comment_id}.json", timeout=10) or {}
        except requests.RequestException:
            return
        if not item.get("deleted") and not item.get("dead"):
            text = clean_text(item.get("text"), 4000)
            if text:
                output.append({
                    "hn_comment_id": item.get("id"),
                    "parent_hn_id": item.get("parent"),
                    "author": item.get("by"),
                    "text": text,
                    "depth": depth,
                    "posted_at": unix_to_sql_time(item.get("time")),
                })
        for child_id in item.get("kids") or []:
            visit(child_id, depth + 1)

    for child_id in root_item.get("kids") or []:
        visit(child_id, 0)
    return output[:limit]


def fetch_comments(conn: sqlite3.Connection, ids: IdAllocator, articles_per_run: int, max_comments: int) -> dict:
    cutoff = sql_time(utc_now() - timedelta(days=2))
    articles = list(conn.execute(
        """SELECT a.id,a.hn_id FROM articles a
           LEFT JOIN (SELECT article_id,MAX(fetched_at) last_fetched_at FROM article_comments GROUP BY article_id) state
             ON state.article_id=a.id
           WHERE a.posted_at>=? AND a.comment_count>0
             AND (state.last_fetched_at IS NULL OR state.last_fetched_at<?)
           ORDER BY state.last_fetched_at IS NULL DESC,a.comment_count DESC LIMIT ?""",
        [cutoff, sql_time(utc_now() - timedelta(hours=1)), articles_per_run],
    ))
    now = sql_time()
    success = failure = 0
    with requests.Session() as session:
        for article in articles:
            try:
                root = fetch_json(session, f"{BASE}/item/{article['hn_id']}.json", timeout=10) or {}
                comments = collect_comments(session, root, max_comments)
                conn.execute("DELETE FROM article_comments WHERE article_id=?", [article["id"]])
                for order, comment in enumerate(comments):
                    conn.execute(
                        """INSERT INTO article_comments(id,article_id,hn_comment_id,parent_hn_id,author,text,depth,
                           display_order,posted_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        [
                            ids.next_id("article_comments"),
                            article["id"],
                            comment["hn_comment_id"],
                            comment["parent_hn_id"],
                            comment["author"],
                            comment["text"],
                            comment["depth"],
                            order,
                            comment["posted_at"],
                            now,
                        ],
                    )
                conn.commit()
                success += 1
            except requests.RequestException:
                conn.rollback()
                failure += 1
    return {"processed": len(articles), "success": success, "failure": failure}


def get_keyword_id(conn: sqlite3.Connection, ids: IdAllocator, word: str, first_seen_at: str) -> int:
    row = conn.execute("SELECT id FROM keywords WHERE word=? ORDER BY id LIMIT 1", [word]).fetchone()
    if row:
        return int(row["id"])
    keyword_id = ids.next_id("keywords")
    conn.execute(
        "INSERT INTO keywords(id,word,first_seen_at,total_appearances) VALUES(?,?,?,0)",
        [keyword_id, word, first_seen_at],
    )
    return keyword_id


def rebuild_keywords(conn: sqlite3.Connection, ids: IdAllocator, days: int) -> dict:
    cutoff = sql_time(utc_now() - timedelta(days=days))
    articles = list(conn.execute(
        """SELECT a.id,a.title,COALESCE(a.category,'Other') category,date(a.posted_at) stat_date,
                  COALESCE(ac.snippet,'') snippet,
                  COALESCE((SELECT GROUP_CONCAT(c.text, ' ')
                            FROM article_comments c WHERE c.article_id=a.id AND c.display_order<10),'') comments
           FROM articles a LEFT JOIN article_content ac ON ac.article_id=a.id
           WHERE a.posted_at>=? AND a.title IS NOT NULL ORDER BY a.fetched_at DESC LIMIT 3000""",
        [cutoff],
    ))
    if not articles:
        return {"processed": 0, "success": 0, "failure": 0}

    titles = [row["title"] for row in articles]
    snippets = [row["snippet"] for row in articles]
    comments = [row["comments"] for row in articles]
    try:
        extracted = extract_weighted_keywords(
            titles,
            snippets,
            comments,
            max_df=STANDARD_MAX_DF,
            min_df=STANDARD_MIN_DF,
        )
    except ValueError as exc:
        return {"processed": len(articles), "success": 0, "failure": len(articles), "error": str(exc)}

    article_ids = [row["id"] for row in articles]
    placeholders = ",".join(["?"] * len(article_ids))
    conn.execute(f"DELETE FROM article_keywords WHERE article_id IN ({placeholders})", article_ids)
    conn.execute("DELETE FROM keyword_daily_stats WHERE stat_date>=date(?)", [cutoff])
    conn.execute("DELETE FROM keyword_category_daily_stats WHERE stat_date>=date(?)", [cutoff])

    daily_counts: Counter[tuple[int, str]] = Counter()
    category_counts: Counter[tuple[int, str, str]] = Counter()
    success = 0
    for article, keywords in zip(articles, extracted):
        stat_date = article["stat_date"] or utc_now().date().isoformat()
        for selected in keywords:
            word = ALIASES.get(str(selected["word"]).strip().lower(), str(selected["word"]).strip().lower())
            keyword_id = get_keyword_id(conn, ids, word, sql_time())
            conn.execute(
                """INSERT INTO article_keywords(article_id,keyword_id,created_at,relevance_score,rank_position)
                   VALUES(?,?,?,?,?)""",
                [article["id"], keyword_id, sql_time(), selected["relevance_score"], selected["rank_position"]],
            )
            daily_counts[(keyword_id, stat_date)] += 1
            category_counts[(keyword_id, stat_date, article["category"])] += 1
        success += 1

    for (keyword_id, stat_date), count in daily_counts.items():
        past = conn.execute(
            "SELECT AVG(appearance_count) FROM keyword_daily_stats WHERE keyword_id=? AND stat_date<?",
            [keyword_id, stat_date],
        ).fetchone()[0]
        novelty = float(count) if not past else float(count) / float(past)
        conn.execute(
            """INSERT INTO keyword_daily_stats(id,keyword_id,stat_date,appearance_count,novelty_score)
               VALUES(?,?,?,?,?)""",
            [ids.next_id("keyword_daily_stats"), keyword_id, stat_date, count, novelty],
        )
    for (keyword_id, stat_date, category), count in category_counts.items():
        conn.execute(
            """INSERT INTO keyword_category_daily_stats(id,keyword_id,stat_date,category,appearance_count)
               VALUES(?,?,?,?,?)""",
            [ids.next_id("keyword_category_daily_stats"), keyword_id, stat_date, category, count],
        )
    for keyword_id, count in Counter(keyword_id for keyword_id, _ in daily_counts).items():
        conn.execute("UPDATE keywords SET total_appearances=? WHERE id=?", [count, keyword_id])

    conn.commit()
    return {"processed": len(articles), "success": success, "failure": 0}


def prune_old_rows(conn: sqlite3.Connection, days: int, keep_batch_runs: int):
    article_cutoff = sql_time(utc_now() - timedelta(days=days))
    old_article_ids = [row["id"] for row in conn.execute("SELECT id FROM articles WHERE posted_at<?", [article_cutoff])]
    if old_article_ids:
        placeholders = ",".join(["?"] * len(old_article_ids))
        for table in ("article_content", "article_keywords", "article_comments", "article_snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE article_id IN ({placeholders})", old_article_ids)
        conn.execute(f"DELETE FROM articles WHERE id IN ({placeholders})", old_article_ids)

    stat_cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    conn.execute("DELETE FROM keyword_daily_stats WHERE stat_date<?", [stat_cutoff])
    conn.execute("DELETE FROM keyword_category_daily_stats WHERE stat_date<?", [stat_cutoff])
    conn.execute(
        """DELETE FROM batch_runs WHERE id NOT IN (
             SELECT id FROM batch_runs
             WHERE job_name IN ({})
             ORDER BY id DESC LIMIT ?
           )""".format(",".join(["?"] * len(ACTIVE_BATCH_JOBS))),
        [*ACTIVE_BATCH_JOBS, keep_batch_runs],
    )
    conn.commit()
    conn.execute("VACUUM")


def ensure_indexes(conn: sqlite3.Connection):
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_articles_hn_id ON articles(hn_id)",
        "CREATE INDEX IF NOT EXISTS idx_keywords_word ON keywords(word)",
        "CREATE INDEX IF NOT EXISTS idx_articles_posted_at ON articles(posted_at)",
        "CREATE INDEX IF NOT EXISTS idx_article_comments_article_order ON article_comments(article_id,display_order)",
        "CREATE INDEX IF NOT EXISTS idx_article_snapshots_article_recorded ON article_snapshots(article_id,recorded_at)",
    ]
    for statement in statements:
        conn.execute(statement)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Update the public SQLite dashboard data from Hacker News.")
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "hn_dashboard.sqlite")
    parser.add_argument("--days", type=int, default=30, help="Recent article/stat window to keep.")
    parser.add_argument("--top-n", type=int, default=500, help="Number of top HN stories to inspect.")
    parser.add_argument("--comment-articles", type=int, default=25, help="Articles whose comments are refreshed per run.")
    parser.add_argument("--max-comments", type=int, default=80, help="Maximum comments per article.")
    parser.add_argument("--keep-batch-runs", type=int, default=200)
    args = parser.parse_args()

    if not args.database.exists():
        raise FileNotFoundError(f"SQLite database not found: {args.database}")

    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    ids = IdAllocator(conn)
    try:
        ensure_indexes(conn)
        results = {
            "fetch_articles": record_batch(conn, ids, "fetch_articles", lambda: fetch_articles(conn, ids, args.top_n)),
            "record_snapshots": record_batch(conn, ids, "record_snapshots", lambda: record_snapshots(conn, ids)),
            "fetch_comments": record_batch(
                conn,
                ids,
                "fetch_comments",
                lambda: fetch_comments(conn, ids, args.comment_articles, args.max_comments),
            ),
            "extract_keywords": record_batch(conn, ids, "extract_keywords", lambda: rebuild_keywords(conn, ids, args.days)),
        }
        prune_old_rows(conn, args.days, args.keep_batch_runs)
    finally:
        conn.close()

    for job, result in results.items():
        print(f"{job}: {result}")
    size_mb = args.database.stat().st_size / 1024 / 1024
    print(f"Updated {args.database} ({size_mb:.2f} MiB)")


if __name__ == "__main__":
    main()
