import html
import ipaddress
import os
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import mysql.connector
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from batch_run import run_recorded

load_dotenv()
BASE = "https://hacker-news.firebaseio.com/v0"
MAX_BYTES = 2_000_000
USER_AGENT = "HNAnalyticsDashboard/1.0"
DB_CONFIG = {
    "host": os.getenv("DB_HOST"), "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASSWORD"), "database": os.getenv("DB_NAME"),
}


def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def clean_text(value, limit=800):
    text = BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:limit]


def is_safe_public_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        return all(not (ipaddress.ip_address(item[4][0]).is_private
                       or ipaddress.ip_address(item[4][0]).is_loopback
                       or ipaddress.ip_address(item[4][0]).is_link_local
                       or ipaddress.ip_address(item[4][0]).is_reserved) for item in addresses)
    except (socket.gaierror, ValueError):
        return False


def allowed_by_robots(session, url):
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = session.get(robots_url, timeout=5, headers={"User-Agent": USER_AGENT}, allow_redirects=False)
        if response.status_code < 400:
            parser.parse(response.text.splitlines())
            return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        pass
    return True


def extract_snippet(html_text, hn_text=""):
    soup = BeautifulSoup(html_text or "", "html.parser")
    og = soup.find("meta", property="og:description")
    if og and clean_text(og.get("content")):
        return clean_text(og.get("content")), "open_graph"
    description = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "description"})
    if description and clean_text(description.get("content")):
        return clean_text(description.get("content")), "meta_description"
    for paragraph in soup.find_all("p"):
        text = clean_text(paragraph.get_text(" ", strip=True))
        if len(text) >= 80:
            return text, "first_paragraph"
    if clean_text(hn_text):
        return clean_text(hn_text), "hn_text"
    return "", None


def fetch_external_snippet(session, url, hn_text):
    if not url:
        snippet, source = extract_snippet("", hn_text)
        return snippet, source, "success" if snippet else "no_content", None
    if not is_safe_public_url(url):
        return "", None, "blocked", "安全でないURLまたは名前解決失敗"
    if not allowed_by_robots(session, url):
        return "", None, "blocked", "robots.txtにより取得不可"
    try:
        current_url = url
        response = None
        for _ in range(4):
            response = session.get(current_url, timeout=12, headers={"User-Agent": USER_AGENT}, stream=True,
                                   allow_redirects=False)
            if response.is_redirect or response.is_permanent_redirect:
                next_url = urljoin(current_url, response.headers.get("Location", ""))
                response.close()
                if not is_safe_public_url(next_url):
                    return "", None, "blocked", "リダイレクト先が安全ではありません"
                current_url = next_url
                continue
            break
        if response is None or response.is_redirect or response.is_permanent_redirect:
            if response is not None:
                response.close()
            return "", None, "failed", "リダイレクト回数上限超過"
        with response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                snippet, source = extract_snippet("", hn_text)
                return snippet, source, "non_html", None
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    return "", None, "failed", "レスポンスサイズ上限超過"
                chunks.append(chunk)
            response.encoding = response.encoding or response.apparent_encoding
            text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            snippet, source = extract_snippet(text, hn_text)
            return snippet, source, "success" if snippet else "no_content", None
    except requests.RequestException as error:
        snippet, source = extract_snippet("", hn_text)
        return snippet, source, "success" if snippet else "failed", str(error)[:500]


def _run():
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT a.id,a.hn_id,a.url
           FROM articles a
           LEFT JOIN article_content ac ON ac.article_id=a.id
           WHERE a.posted_at>=NOW()-INTERVAL 2 DAY
             AND (ac.article_id IS NULL OR ac.fetch_status IN ('pending','failed'))
           ORDER BY a.score DESC LIMIT 100"""
    )
    articles = cur.fetchall()
    success = failure = 0
    with requests.Session() as session:
        for article in articles:
            hn_text = ""
            try:
                item = session.get(f"{BASE}/item/{article['hn_id']}.json", timeout=10).json() or {}
                hn_text = clean_text(item.get("text"), 2000)
            except requests.RequestException:
                pass
            snippet, source, status, error = fetch_external_snippet(session, article["url"], hn_text)
            cur.execute(
                """INSERT INTO article_content(article_id,snippet,snippet_source,hn_text,fetch_status,fetched_at,error_message)
                   VALUES(%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE snippet=VALUES(snippet),
                   snippet_source=VALUES(snippet_source),hn_text=VALUES(hn_text),fetch_status=VALUES(fetch_status),
                   fetched_at=VALUES(fetched_at),error_message=VALUES(error_message)""",
                [article["id"], snippet or None, source, hn_text or None, status,
                 datetime.now(timezone.utc).replace(tzinfo=None), error],
            )
            success += status in ("success", "no_content", "non_html")
            failure += status in ("failed", "blocked")
            time.sleep(0.3)
    conn.commit()
    cur.close()
    conn.close()
    return {"processed": len(articles), "success": success, "failure": failure}


def main():
    return run_recorded("enrich_articles", get_conn, _run)


if __name__ == "__main__":
    main()
