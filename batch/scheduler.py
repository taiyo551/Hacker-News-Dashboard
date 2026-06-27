import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from extract_keywords import main as extract_keywords
from fetch_articles import main as fetch_articles
from record_snapshots import main as record_snapshots
from summarize_articles import main as summarize_articles
from enrich_articles import main as enrich_articles
from fetch_comments import main as fetch_comments

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
scheduler = BlockingScheduler(timezone="Asia/Tokyo")
scheduler.add_job(fetch_articles, "interval", minutes=30, next_run_time=datetime.now())
scheduler.add_job(record_snapshots, "interval", minutes=30)
scheduler.add_job(extract_keywords, "interval", minutes=30)
scheduler.add_job(summarize_articles, "interval", hours=2)
scheduler.add_job(enrich_articles, "interval", hours=2)
scheduler.add_job(fetch_comments, "interval", hours=1)

if __name__ == "__main__":
    logging.info("Scheduler started")
    scheduler.start()
