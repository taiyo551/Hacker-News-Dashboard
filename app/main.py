from datetime import datetime, timezone
from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from analysis import is_rankable_keyword, keyword_attention_score
from db import (
    article_source,
    fetch_df,
    get_article_comments,
    get_article_keywords,
    get_article_momentum,
    get_article_snapshots,
    get_category_momentum,
    get_keyword_category_trends,
    get_keyword_shifts,
    get_keyword_trends,
    get_latest_batch_runs,
    get_pipeline_health,
    get_public_articles,
    get_public_related_articles,
    get_recent_batch_runs,
    get_story_clusters,
    get_trending_keyword_count,
)

CATEGORIES = [
    "All", "AI", "Security", "Programming", "Web", "OS", "Startup",
    "Database", "DevOps", "Hardware", "Science", "Policy", "Other",
]
ARTICLE_TYPES = ["All", "story", "ask", "show", "job"]
SORT_OPTIONS = {
    "Importance": "importance",
    "Newest": "new",
    "HN score": "score",
    "Comments": "comments",
    "Momentum": "rising",
}
ARTICLE_TYPE_LABELS = {
    "All": "All posts",
    "story": "Stories",
    "ask": "Ask HN",
    "show": "Show HN",
    "job": "Jobs",
}

st.set_page_config(page_title="HN Tech Trend Radar", layout="wide")


def to_jst(value):
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.tz_localize("UTC").tz_convert("Asia/Tokyo") if timestamp.tzinfo is None else timestamp.tz_convert("Asia/Tokyo")


def minutes_ago(value):
    timestamp = to_jst(value)
    if timestamp is None:
        return "-"
    age = datetime.now(timezone.utc) - timestamp.to_pydatetime().astimezone(timezone.utc)
    minutes = max(0, int(age.total_seconds() // 60))
    return f"{minutes} min ago" if minutes < 180 else f"{minutes // 60} hr ago"


def split_keywords(value, limit=5):
    return [keyword.strip() for keyword in str(value or "").split(",") if keyword.strip()][:limit]


def metric_number(value):
    return f"{int(value or 0):,}"


@st.cache_data(ttl=300, show_spinner=False)
def load_today_count():
    rows = fetch_df(
        """SELECT COUNT(*) count, MAX(fetched_at) latest_fetch
           FROM articles
           WHERE posted_at>=datetime(
             (SELECT COALESCE(MAX(COALESCE(fetched_at, posted_at)), CURRENT_TIMESTAMP) FROM articles),
             '-1 day'
           )"""
    )
    if rows.empty:
        return 0, None
    return int(rows.iloc[0]["count"] or 0), rows.iloc[0]["latest_fetch"]


@st.cache_data(ttl=300, show_spinner=False)
def load_articles(days, category, article_type, min_score, min_comments, query, sort_by, limit):
    return get_public_articles(
        days=days,
        category=category,
        article_type=article_type,
        min_score=min_score,
        min_comments=min_comments,
        search_text=query,
        sort_by=sort_by,
        limit=limit,
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_health(days):
    return get_pipeline_health(days)


@st.cache_data(ttl=300, show_spinner=False)
def load_momentum(hours, limit):
    return get_article_momentum(hours, limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_keyword_trends(days):
    return get_keyword_trends(days)


@st.cache_data(ttl=300, show_spinner=False)
def load_keyword_shifts(window_days, limit):
    return get_keyword_shifts(window_days, limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_category_momentum(hours):
    return get_category_momentum(hours)


@st.cache_data(ttl=300, show_spinner=False)
def load_story_clusters(days, limit):
    return get_story_clusters(days, limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_article_keywords(article_id):
    return get_article_keywords(article_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_article_snapshots(article_id):
    return get_article_snapshots(article_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_article_comments(article_id, limit):
    return get_article_comments(article_id, limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_related_articles(article_id, days, limit):
    return get_public_related_articles(article_id, days, limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_latest_runs():
    return get_latest_batch_runs()


@st.cache_data(ttl=300, show_spinner=False)
def load_recent_runs(limit):
    return get_recent_batch_runs(limit)


@st.cache_data(ttl=300, show_spinner=False)
def load_trending_keyword_count():
    return get_trending_keyword_count()


def stop_on_data_error(error):
    st.error("Could not load the dashboard data. Check that the SQLite snapshot exists and is readable.")
    st.caption(str(error))
    st.stop()


def article_reason(row):
    parts = []
    velocity = float(row.get("velocity") or 0)
    if velocity > 0:
        parts.append(f"24h gain {velocity:.0f}")
    if int(row.get("comment_count") or 0) >= 50:
        parts.append("active discussion")
    if int(row.get("score") or 0) >= 100:
        parts.append("high HN score")
    return " / ".join(parts) or "fresh and active"


def render_article_stats(score, comments, velocity, importance):
    stats = [
        ("HN Score", score, "Hacker News score for the story."),
        ("Comments", comments, "Number of Hacker News comments."),
        ("24h Change", f"{velocity:.0f}", "Observed score and comment movement over the last 24 hours."),
        ("Importance", f"{importance:.0f}", "Composite score based on HN score, comments, and momentum."),
    ]
    cells = []
    for label, value, title in stats:
        cells.append(
            "<div "
            f"title=\"{escape(title)}\" "
            "style=\"border:1px solid rgba(148,163,184,.25);border-radius:6px;"
            "padding:.35rem .45rem;min-width:0;\">"
            f"<div style=\"font-size:.68rem;color:#94a3b8;line-height:1.05;white-space:normal;\">{escape(str(label))}</div>"
            f"<div style=\"font-size:1rem;font-weight:700;line-height:1.15;margin-top:.12rem;\">{escape(str(value))}</div>"
            "</div>"
        )
    st.markdown(
        "<div style=\"display:grid;grid-template-columns:repeat(2,minmax(0,1fr));"
        "gap:.35rem;margin-top:.15rem;\">"
        + "".join(cells)
        + "</div>",
        unsafe_allow_html=True,
    )


def build_snapshot_chart_frame(row, snapshots):
    if snapshots.empty:
        timestamp = pd.to_datetime(row.get("fetched_at") or row.get("posted_at"), errors="coerce")
        if pd.isna(timestamp):
            timestamp = pd.Timestamp.now()
        return pd.DataFrame([{
            "recorded_at": timestamp,
            "score": int(row.get("score") or 0),
            "comment_count": int(row.get("comment_count") or 0),
        }]), False

    chart_data = snapshots.copy()
    chart_data["recorded_at"] = pd.to_datetime(chart_data["recorded_at"], errors="coerce")
    chart_data = chart_data.dropna(subset=["recorded_at"])
    if chart_data.empty:
        return build_snapshot_chart_frame(row, pd.DataFrame())
    chart_data["score"] = pd.to_numeric(chart_data["score"], errors="coerce").fillna(0)
    chart_data["comment_count"] = pd.to_numeric(chart_data["comment_count"], errors="coerce").fillna(0)
    recent = chart_data[chart_data["recorded_at"] >= chart_data["recorded_at"].max() - pd.Timedelta(hours=24)]
    return (recent if not recent.empty else chart_data), True


def hourly_tick_values(values):
    timestamps = pd.to_datetime(pd.Series(values), errors="coerce").dropna()
    if timestamps.empty:
        return []
    start = timestamps.min().floor("h")
    end = timestamps.max().ceil("h")
    if start == end:
        end = start + pd.Timedelta(hours=1)
    return pd.date_range(start, end, freq="h").to_pydatetime().tolist()


def render_detail(row, article_id, days):
    summary = str(row.get("summary_ja") or "").strip()
    snippet = str(row.get("snippet") or "").strip()
    if summary:
        st.markdown("**Japanese summary**")
        st.write(summary)
    if snippet:
        st.markdown("**Article snippet**")
        st.write(snippet)

    keywords = load_article_keywords(article_id)
    if not keywords.empty:
        st.markdown("**Keywords**")
        st.dataframe(
            keywords[["word", "relevance_score", "rank_position"]].rename(
                columns={"word": "Keyword", "relevance_score": "Relevance", "rank_position": "Rank"}
            ),
            hide_index=True,
            width="stretch",
        )

    snapshots = load_article_snapshots(article_id)
    chart_data, has_history = build_snapshot_chart_frame(row, snapshots)
    long_snapshots = chart_data.melt("recorded_at", ["score", "comment_count"], var_name="metric", value_name="value")
    st.altair_chart(
        alt.Chart(long_snapshots).mark_line(point=True).encode(
            x=alt.X(
                "recorded_at:T",
                title="Time",
                axis=alt.Axis(format="%H:%M", values=hourly_tick_values(chart_data["recorded_at"])),
            ),
            y=alt.Y("value:Q", title="Value"),
            color=alt.Color("metric:N", title="Metric"),
            tooltip=["recorded_at:T", "metric:N", "value:Q"],
        ).properties(height=220),
        width="stretch",
    )
    if not has_history:
        st.caption("Only the current HN score and comment values are available for this story.")

    comments = load_article_comments(article_id, 8)
    if not comments.empty:
        st.markdown("**HN comments**")
        for _, comment in comments.iterrows():
            st.caption(comment.get("author") or "-")
            st.write(comment["text"])

    related = load_related_articles(article_id, max(days, 30), 5)
    if not related.empty:
        st.markdown("**Related articles**")
        for _, related_row in related.iterrows():
            st.markdown(f"[{related_row['title']}]({related_row.get('url') or '#'})")
            st.caption(
                f"{related_row.get('recommendation_reason') or '-'} / "
                f"Importance {float(related_row.get('importance_score') or 0):.0f}"
            )


def article_card(row, prefix, days):
    article_id = int(row["id"])
    posted = to_jst(row.get("posted_at"))
    source = article_source(row.get("url"))
    keywords = split_keywords(row.get("keyword_list"), 4)
    score = int(row.get("score") or 0)
    comments = int(row.get("comment_count") or 0)
    velocity = float(row.get("velocity") or 0)
    importance = float(row.get("importance_score") or 0)
    detail_key = f"{prefix}_detail_{article_id}"

    with st.container(border=True):
        title, metrics = st.columns([6, 4])
        with title:
            st.markdown(f"##### [{row.get('title') or '(no title)'}]({row.get('url') or '#'})")
            chips = [str(row.get("category") or "Other"), source]
            chips.extend(keywords)
            st.caption(" / ".join(chips))
            if posted is not None:
                st.caption(f"Posted {posted.strftime('%m/%d %H:%M')} JST / {article_reason(row)}")
        with metrics:
            render_article_stats(score, comments, velocity, importance)

        snippet = str(row.get("snippet") or "").strip()
        if snippet:
            st.caption(snippet[:220] + ("..." if len(snippet) > 220 else ""))

        if st.button("Analyze", key=f"{detail_key}_button"):
            st.session_state[detail_key] = not st.session_state.get(detail_key, False)

        if st.session_state.get(detail_key, False):
            with st.expander("Article analysis", expanded=True):
                try:
                    render_detail(row, article_id, days)
                except Exception as error:
                    st.warning("Could not load the detail data.")
                    st.caption(str(error))


def build_keyword_attention(trends):
    if trends.empty:
        return pd.DataFrame()
    trends = trends.copy()
    trends["stat_date"] = pd.to_datetime(trends["stat_date"])
    latest = trends.sort_values("stat_date").groupby("keyword_id").tail(1).copy()
    first_seen = pd.to_datetime(latest["first_seen_at"], errors="coerce").dt.tz_localize(None)
    latest["is_new"] = (latest["stat_date"] - first_seen).dt.days <= 3
    latest["Type"] = latest["is_new"].map({True: "New", False: "Rising"})
    latest = latest[latest["word"].map(is_rankable_keyword)].copy()
    latest["Attention"] = latest.apply(
        lambda item: keyword_attention_score(item["appearance_count"], item["novelty_score"], item["word"]),
        axis=1,
    )
    return latest.sort_values("Attention", ascending=False)


def bounded_percent(value):
    return max(0, min(100, int(round(float(value or 0)))))


def freshness_notes(health):
    notes = []
    if health["keyword_coverage"] >= 70:
        notes.append("Most stories have extracted keywords, so trend charts should be useful.")
    else:
        notes.append("Keyword extraction is still catching up, so trend charts may undercount some topics.")
    if health["comment_coverage"] >= 50:
        notes.append("Comment coverage is broad enough to sample active discussions.")
    else:
        notes.append("Comment coverage is partial, so story discussion summaries may be uneven.")
    if health["snapshot_coverage"] >= 50:
        notes.append("Momentum charts have enough score snapshots to show movement.")
    else:
        notes.append("Momentum is based on limited snapshots and may favor newly popular stories.")
    return notes


with st.sidebar:
    st.header("Filters")
    days = st.selectbox("Time window", [1, 3, 7, 14, 30], index=2, format_func=lambda value: f"{value} days")
    category = st.selectbox("Category", CATEGORIES)
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()

st.title("HN Tech Trend Radar")
st.caption("A public read-only view of technical trends on Hacker News.")

try:
    today_count, latest_fetch = load_today_count()
    health = load_health(days)
    trend_count = load_trending_keyword_count()
    overview_articles = load_articles(
        days,
        category,
        "All",
        0,
        0,
        "",
        "importance",
        8,
    )
except Exception as error:
    stop_on_data_error(error)

status_columns = st.columns(4)
status_columns[0].metric("Last updated", minutes_ago(latest_fetch))
status_columns[1].metric("New today", metric_number(today_count))
status_columns[2].metric(f"{days}-day articles", metric_number(health["article_count"]))
status_columns[3].metric("Rising terms", metric_number(trend_count))

focus_tab, explore_tab, trend_tab, ops_tab = st.tabs(["Overview", "Articles", "Trends", "Data freshness"])

with focus_tab:
    st.subheader("Fast-moving stories")
    try:
        momentum = load_momentum(24, 12)
    except Exception as error:
        momentum = pd.DataFrame()
        st.warning("Could not load momentum data.")
        st.caption(str(error))
    if not momentum.empty and category != "All":
        momentum = momentum[momentum["category"] == category].copy()
    if momentum.empty:
        st.info("No fast-moving stories are available for the last 24 hours yet.")
    else:
        momentum = momentum.copy()
        momentum["Confidence"] = pd.to_numeric(momentum["snapshot_count"], errors="coerce").fillna(0).map(
            lambda count: "High" if count >= 3 else ("Medium" if count >= 2 else "Low")
        )
        st.altair_chart(
            alt.Chart(momentum.head(10)).mark_bar().encode(
                x=alt.X("momentum_score:Q", title="Momentum"),
                y=alt.Y("title:N", sort="-x", title=None),
                color=alt.Color(
                    "Confidence:N",
                    title="Confidence",
                    scale=alt.Scale(domain=["High", "Medium", "Low"], range=["#16a34a", "#f59e0b", "#64748b"]),
                ),
                tooltip=["title:N", "category:N", "score:Q", "comment_count:Q", "snapshot_count:Q"],
            ).properties(height=320),
            width="stretch",
        )

    st.subheader("Top stories right now")
    if overview_articles.empty:
        st.info("No stories match the current filters.")
    else:
        for _, row in overview_articles.iterrows():
            article_card(row, "focus", days)

    st.subheader("Themes appearing across sources")
    try:
        clusters = load_story_clusters(days, 8)
    except Exception as error:
        clusters = pd.DataFrame()
        st.warning("Could not load theme clusters.")
        st.caption(str(error))
    if clusters.empty:
        st.caption("No related title clusters have been detected yet.")
    else:
        st.dataframe(
            clusters[["lead_title", "category", "article_count", "source_count", "total_score", "sources"]].rename(
                columns={
                    "lead_title": "Lead story",
                    "category": "Category",
                    "article_count": "Stories",
                    "source_count": "Sources",
                    "total_score": "Total HN",
                    "sources": "Source domains",
                }
            ),
            hide_index=True,
            width="stretch",
        )

with explore_tab:
    st.subheader("Articles")
    st.caption("Search, filter, compare, and expand individual stories.")
    query = st.text_input("Search articles", placeholder="python security")
    filter_cols = st.columns([2, 2, 2])
    sort_label = filter_cols[0].selectbox("Sort by", list(SORT_OPTIONS.keys()))
    article_type = filter_cols[1].selectbox(
        "Post type",
        ARTICLE_TYPES,
        format_func=lambda value: ARTICLE_TYPE_LABELS[value],
        help="HN item type: stories, Ask HN posts, Show HN posts, or job posts.",
    )
    result_limit = filter_cols[2].slider("Articles shown", 5, 50, 20, step=5)
    threshold_cols = st.columns(2)
    min_score = threshold_cols[0].slider("Minimum HN score", 0, 500, 0, step=10)
    min_comments = threshold_cols[1].slider("Minimum comments", 0, 300, 0, step=10)

    try:
        article_results = load_articles(
            days,
            category,
            article_type,
            min_score,
            min_comments,
            query,
            SORT_OPTIONS[sort_label],
            result_limit,
        )
    except Exception as error:
        article_results = pd.DataFrame()
        st.warning("Could not load article results.")
        st.caption(str(error))

    if article_results.empty:
        st.info("No stories match the current filters.")
    else:
        export_columns = [
            "title", "url", "category", "type", "score", "comment_count",
            "importance_score", "velocity", "posted_at", "keyword_list",
        ]
        st.download_button(
            "Download CSV",
            article_results[export_columns].to_csv(index=False).encode("utf-8-sig"),
            "hn_public_articles.csv",
            "text/csv",
        )
        for _, row in article_results.iterrows():
            article_card(row, "explore", days)

with trend_tab:
    trend_days = st.selectbox(
        "Analysis window", [1, 2, 3, 7, 14, 30], index=3,
        format_func=lambda value: f"{value * 24} hours" if value <= 3 else f"{value} days",
    )
    try:
        trends = load_keyword_trends(trend_days)
        shifts = load_keyword_shifts(3, 60)
        category_momentum = load_category_momentum(24)
    except Exception as error:
        st.warning("Could not load trend data.")
        st.caption(str(error))
        trends = pd.DataFrame()
        shifts = pd.DataFrame()
        category_momentum = pd.DataFrame()

    latest = build_keyword_attention(trends)
    if latest.empty:
        st.info("No notable terms are available yet.")
    else:
        st.subheader("Notable terms")
        st.altair_chart(
            alt.Chart(latest.head(12)).mark_bar().encode(
                x=alt.X("Attention:Q", title="Attention"),
                y=alt.Y("word:N", sort="-x", title=None),
                color=alt.Color("Type:N", title="Type", scale=alt.Scale(range=["#f97316", "#2563eb"])),
                tooltip=[
                    "word:N", "Type:N", "appearance_count:Q",
                    alt.Tooltip("novelty_score:Q", format=".2f"),
                    alt.Tooltip("Attention:Q", format=".2f"),
                ],
            ).properties(height=340),
            width="stretch",
        )

        selected = st.selectbox("Term history", latest["word"].head(25).tolist())
        selected_id = int(latest.loc[latest["word"] == selected, "keyword_id"].iloc[0])
        line_data = trends[trends["keyword_id"] == selected_id].copy()
        st.altair_chart(
            alt.Chart(line_data).mark_line(point=True).encode(
                x=alt.X("stat_date:T", title="Date"),
                y=alt.Y("appearance_count:Q", title="Appearances"),
                tooltip=["stat_date:T", "appearance_count:Q", alt.Tooltip("novelty_score:Q", format=".2f")],
            ).properties(height=240),
            width="stretch",
        )
        categories = get_keyword_category_trends(trend_days, selected_id)
        if not categories.empty:
            st.altair_chart(
                alt.Chart(categories).mark_bar().encode(
                    x=alt.X("stat_date:T", title="Date"),
                    y=alt.Y("appearance_count:Q", title="Appearances"),
                    color=alt.Color("category:N", title="Category"),
                    tooltip=["stat_date:T", "category:N", "appearance_count:Q"],
                ).properties(height=240),
                width="stretch",
            )

    left, right = st.columns(2)
    with left:
        st.subheader("Category movement")
        if category_momentum.empty:
            st.info("No category comparison data is available.")
        else:
            st.altair_chart(
                alt.Chart(category_momentum.head(12)).mark_bar().encode(
                    x=alt.X("momentum:Q", title="Movement"),
                    y=alt.Y("category:N", sort="-x", title=None),
                    color=alt.condition("datum.momentum >= 0", alt.value("#16a34a"), alt.value("#dc2626")),
                    tooltip=[
                        "category:N", "current_count:Q", "previous_count:Q",
                        alt.Tooltip("momentum:Q", format=".1f"),
                    ],
                ).properties(height=320),
                width="stretch",
            )
    with right:
        st.subheader("Rising and cooling terms")
        if shifts.empty:
            st.caption("No comparable terms are available yet.")
        else:
            shifts = shifts.copy()
            shifts["change_percent"] = pd.to_numeric(shifts["change_percent"], errors="coerce")
            movement = shifts[(shifts["change_percent"] >= 50) | (shifts["change_percent"] <= -40)].head(15)
            movement = movement[movement["word"].map(is_rankable_keyword)].copy()
            if movement.empty:
                st.caption("No displayable terms changed sharply in this window.")
            else:
                st.dataframe(
                    movement[["word", "current_count", "previous_count", "change_percent"]].rename(
                        columns={
                            "word": "Term",
                            "current_count": "Current",
                            "previous_count": "Previous",
                            "change_percent": "Change %",
                        }
                    ),
                    hide_index=True,
                    width="stretch",
                )

with ops_tab:
    st.subheader("Data freshness")
    st.caption("These checks explain how complete the public analysis is for the current sidebar time window.")

    freshness_cols = st.columns(4)
    freshness_cols[0].metric("Last updated", minutes_ago(latest_fetch))
    freshness_cols[1].metric("Stories analyzed", metric_number(health["article_count"]))
    freshness_cols[2].metric("Keyword coverage", f"{health['keyword_coverage']:.0f}%")
    freshness_cols[3].metric("Comment coverage", f"{health['comment_coverage']:.0f}%")

    st.markdown("**Coverage**")
    coverage_items = [
        ("Keywords", health["keyword_coverage"], "Used for trend ranking and related terms."),
        ("Categories", health["classified_coverage"], "Used for category filters and category movement."),
        ("Snapshots", health["snapshot_coverage"], "Used for momentum and 24-hour movement."),
        ("Comments", health["comment_coverage"], "Used for discussion context inside story analysis."),
    ]
    for label, value, description in coverage_items:
        st.caption(f"{label}: {description}")
        st.progress(bounded_percent(value), text=f"{label}: {float(value or 0):.0f}%")

    st.markdown("**How to read this**")
    for note in freshness_notes(health):
        st.write(f"- {note}")

    with st.expander("Technical pipeline log"):
        try:
            latest_runs = load_latest_runs()
            recent_runs = load_recent_runs(12)
        except Exception as error:
            latest_runs = pd.DataFrame()
            recent_runs = pd.DataFrame()
            st.warning("Could not load the pipeline history.")
            st.caption(str(error))

        if not latest_runs.empty:
            st.markdown("**Latest runs**")
            st.dataframe(
                latest_runs[["job_name", "status", "finished_at", "success_count", "failure_count"]],
                hide_index=True,
                width="stretch",
            )
        if not recent_runs.empty:
            st.markdown("**Recent run history**")
            st.dataframe(recent_runs, hide_index=True, width="stretch")
