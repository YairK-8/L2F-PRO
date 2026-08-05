import json
import uuid
from threading import Lock

from backend.realtime import emit_to_device, record_error_event, socketio
from database.db import get_connection

JOB_TYPE_MORNING_SCAN = "missing_floor_scan"
JOB_TYPE_WAREHOUSE_SCAN = "missing_warehouse_scan"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"

SCAN_JOB_RESULT_EVENT = "scan_job_result"
WORKER_IDLE_SLEEP_SECONDS = 0.08
WORKER_RETRY_SLEEP_SECONDS = 0.5

_worker_started = False
_worker_lock = Lock()


def _ensure_scan_queue_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            branch_id INTEGER NOT NULL,
            device_id TEXT NOT NULL DEFAULT '',
            device_name TEXT NOT NULL DEFAULT '',
            job_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_jobs_status_id ON scan_jobs(status, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_jobs_branch_device ON scan_jobs(branch_id, device_id, id)"
    )


def ensure_scan_queue_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    socketio.start_background_task(_scan_queue_worker)


def enqueue_scan_job(branch_id, device_id, device_name, job_type, payload):
    ensure_scan_queue_worker()

    request_id = uuid.uuid4().hex
    payload_json = json.dumps(payload or {}, ensure_ascii=False)

    conn = get_connection()
    try:
        _ensure_scan_queue_schema(conn)
        conn.execute(
            """
            INSERT INTO scan_jobs (
                request_id, branch_id, device_id, device_name, job_type, payload_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                int(branch_id),
                str(device_id or "").strip(),
                str(device_name or "").strip(),
                str(job_type or "").strip(),
                payload_json,
                JOB_STATUS_QUEUED,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "request_id": request_id,
        "job_type": job_type,
        "queued": True,
        "accepted": True,
    }


def _claim_next_job():
    conn = get_connection()
    try:
        _ensure_scan_queue_schema(conn)
        row = conn.execute(
            """
            SELECT *
            FROM scan_jobs
            WHERE status=?
            ORDER BY id ASC
            LIMIT 1
            """,
            (JOB_STATUS_QUEUED,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE scan_jobs
            SET status=?, started_at=datetime('now','localtime')
            WHERE id=? AND status=?
            """,
            (JOB_STATUS_PROCESSING, row["id"], JOB_STATUS_QUEUED),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def _finish_job(job, status_code, response):
    payload = response if isinstance(response, dict) else {"error": "invalid_response"}
    status = JOB_STATUS_DONE if 200 <= int(status_code) < 400 else JOB_STATUS_FAILED

    conn = get_connection()
    try:
        _ensure_scan_queue_schema(conn)
        conn.execute(
            """
            UPDATE scan_jobs
            SET status=?, result_json=?, finished_at=datetime('now','localtime')
            WHERE id=?
            """,
            (
                status,
                json.dumps(payload, ensure_ascii=False),
                job["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if status == JOB_STATUS_FAILED:
        record_error_event(
            "scan_job_failed",
            f"{job['job_type']}:{job['request_id']} -> {payload.get('error', status_code)}",
            "queue",
        )

    device_id = str(job.get("device_id") or "").strip()
    if device_id:
        emit_to_device(
            int(job["branch_id"]),
            device_id,
            SCAN_JOB_RESULT_EVENT,
            {
                "request_id": job["request_id"],
                "job_type": job["job_type"],
                "status_code": int(status_code),
                "response": payload,
            },
        )


def _process_job(job):
    payload = json.loads(job.get("payload_json") or "{}")
    branch_id = int(job["branch_id"])
    job_type = str(job["job_type"] or "")

    if job_type == JOB_TYPE_MORNING_SCAN:
        from backend.missing_floor import process_morning_scan_request

        return process_morning_scan_request(branch_id, payload)

    if job_type == JOB_TYPE_WAREHOUSE_SCAN:
        from backend.missing_warehouse import process_missing_warehouse_scan_request

        return process_missing_warehouse_scan_request(branch_id, payload)

    return {"error": "unsupported_job_type"}, 400


def _scan_queue_worker():
    while True:
        try:
            job = _claim_next_job()
            if not job:
                socketio.sleep(WORKER_IDLE_SLEEP_SECONDS)
                continue

            try:
                response, status_code = _process_job(job)
            except Exception as exc:
                record_error_event(
                    "scan_job_exception",
                    f"{job.get('job_type')}:{job.get('request_id')} -> {exc}",
                    "queue",
                )
                response, status_code = {"error": "server_error"}, 500

            _finish_job(job, status_code, response)
        except Exception as exc:
            record_error_event("scan_queue_worker", str(exc), "queue")
            socketio.sleep(WORKER_RETRY_SLEEP_SECONDS)
