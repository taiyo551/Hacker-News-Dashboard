CREATE TABLE IF NOT EXISTS batch_runs (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  job_name VARCHAR(100) NOT NULL,
  started_at DATETIME NOT NULL,
  finished_at DATETIME NULL,
  status ENUM('running','success','partial','failed') NOT NULL DEFAULT 'running',
  processed_count INT NOT NULL DEFAULT 0,
  success_count INT NOT NULL DEFAULT 0,
  failure_count INT NOT NULL DEFAULT 0,
  error_message TEXT NULL,
  INDEX idx_batch_runs_job_finished (job_name, finished_at)
);

CREATE TABLE IF NOT EXISTS keyword_category_daily_stats (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  keyword_id BIGINT UNSIGNED NOT NULL,
  stat_date DATE NOT NULL,
  category VARCHAR(100) NOT NULL,
  appearance_count INT NOT NULL DEFAULT 0,
  UNIQUE KEY uq_keyword_category_date (keyword_id, stat_date, category),
  CONSTRAINT fk_kcds_keyword FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article_content (
  article_id BIGINT UNSIGNED PRIMARY KEY,
  snippet TEXT NULL,
  snippet_source VARCHAR(30) NULL,
  hn_text TEXT NULL,
  fetch_status ENUM('pending','success','no_content','non_html','blocked','failed') NOT NULL DEFAULT 'pending',
  fetched_at DATETIME NULL,
  error_message TEXT NULL,
  CONSTRAINT fk_article_content_article FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article_comments (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  article_id BIGINT UNSIGNED NOT NULL,
  hn_comment_id BIGINT UNSIGNED NOT NULL,
  parent_hn_id BIGINT UNSIGNED NULL,
  author VARCHAR(255) NULL,
  text TEXT NOT NULL,
  depth INT NOT NULL DEFAULT 0,
  display_order INT NOT NULL,
  posted_at DATETIME NULL,
  fetched_at DATETIME NOT NULL,
  UNIQUE KEY uq_article_comment_hn_id (hn_comment_id),
  INDEX idx_article_comments_article_order (article_id, display_order),
  CONSTRAINT fk_article_comments_article FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);
