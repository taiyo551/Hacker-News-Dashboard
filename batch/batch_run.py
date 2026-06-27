from datetime import datetime, timezone
import traceback


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_recorded(job_name, get_conn, worker):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE batch_runs SET finished_at=%s,status='failed',failure_count=GREATEST(failure_count,1),
           error_message=COALESCE(error_message,'前回プロセスが完了記録なしで終了しました')
           WHERE job_name=%s AND status='running'""",
        [utc_now(), job_name],
    )
    cur.execute("INSERT INTO batch_runs(job_name,started_at,status) VALUES(%s,%s,'running')", [job_name, utc_now()])
    run_id = cur.lastrowid
    conn.commit()
    processed = success = failure = 0
    status, error = "failed", None
    try:
        result = worker() or {}
        processed = int(result.get("processed", 0))
        success = int(result.get("success", processed))
        failure = int(result.get("failure", max(processed - success, 0)))
        status = "partial" if failure and success else ("failed" if failure else "success")
        error = result.get("error")
        return result
    except Exception:
        failure = max(failure, 1)
        error = traceback.format_exc(limit=5)
        raise
    finally:
        cur.execute(
            """UPDATE batch_runs SET finished_at=%s,status=%s,processed_count=%s,
               success_count=%s,failure_count=%s,error_message=%s WHERE id=%s""",
            [utc_now(), status, processed, success, failure, error, run_id],
        )
        conn.commit()
        cur.close()
        conn.close()
