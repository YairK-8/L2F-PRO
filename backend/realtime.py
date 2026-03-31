from collections import defaultdict, deque
from threading import Lock
import time

from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from database.db import get_connection

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")

HEARTBEAT_TIMEOUT_SECONDS = 15 * 60
HEALTH_WINDOW_SECONDS = 5 * 60
HEARTBEAT_CHECK_INTERVAL_SECONDS = 60
ERROR_BUFFER_LIMIT = 100

_sid_to_conn = {}
_branch_to_sids = defaultdict(set)
_request_metrics = deque()
_scan_events = deque()
_error_events = deque()
_socket_connect_events = deque()
_socket_disconnect_events = deque()
_emit_events = deque()
_server_started_at = time.time()
_cleanup_task_started = False
_lock = Lock()


def branch_room(branch_id: int) -> str:
    return f"branch_{branch_id}"


def _now() -> float:
    return time.time()


def _trim_deque(items: deque, window_seconds: int = HEALTH_WINDOW_SECONDS):
    cutoff = _now() - window_seconds
    while items and items[0][0] < cutoff:
        items.popleft()


def _device_view(conn: dict) -> dict:
    return {
        "device_id": conn["device_id"],
        "device_name": conn["device_name"],
        "branch_id": conn["branch_id"],
        "connected_at": conn["connected_at"],
        "last_seen": conn["last_seen"],
        "socket_count": conn["socket_count"],
        "stale": bool(conn.get("stale")),
    }


def _is_device_blocked(branch_id: int, device_id: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM blocked_devices WHERE branch_id=? AND device_id=?",
        (branch_id, device_id)
    ).fetchone()
    conn.close()
    return bool(row)


def _rebuild_branch_summary_locked() -> dict[int, dict]:
    summary = {}
    for branch_id, sids in _branch_to_sids.items():
        devices = {}
        for sid in list(sids):
            conn = _sid_to_conn.get(sid)
            if not conn:
                continue
            device_id = conn["device_id"]
            item = devices.get(device_id)
            if not item:
                devices[device_id] = {
                    "device_id": device_id,
                    "device_name": conn["device_name"],
                    "branch_id": branch_id,
                    "connected_at": conn["connected_at"],
                    "last_seen": conn["last_seen"],
                    "socket_count": 1,
                    "stale": False,
                }
                continue
            item["socket_count"] += 1
            if conn["connected_at"] < item["connected_at"]:
                item["connected_at"] = conn["connected_at"]
            if conn["last_seen"] > item["last_seen"]:
                item["last_seen"] = conn["last_seen"]
        if devices:
            summary[branch_id] = {
                "active_devices": len(devices),
                "active_sockets": len(sids),
                "devices": sorted(
                    (_device_view(item) for item in devices.values()),
                    key=lambda d: (d["device_name"].lower(), d["device_id"]),
                ),
            }
    return summary


def _remove_sid_locked(sid: str):
    conn = _sid_to_conn.pop(sid, None)
    if not conn:
        return None
    branch_id = conn["branch_id"]
    sids = _branch_to_sids.get(branch_id)
    if sids is not None:
        sids.discard(sid)
        if not sids:
            _branch_to_sids.pop(branch_id, None)
    _socket_disconnect_events.append((_now(), {
        "sid": sid,
        "branch_id": branch_id,
        "device_id": conn["device_id"],
        "reason": conn.get("disconnect_reason", "disconnect"),
    }))
    _trim_deque(_socket_disconnect_events)
    return conn


def _schedule_force_disconnect(sids: list[str], reason: str):
    def _worker():
        socketio.sleep(0.35)
        for sid in sids:
            try:
                socketio.server.disconnect(sid, namespace="/")
            except Exception:
                record_error_event("socket_disconnect_failed", f"sid={sid}", "realtime")
    socketio.start_background_task(_worker)


def _cleanup_stale_connections():
    while True:
        socketio.sleep(HEARTBEAT_CHECK_INTERVAL_SECONDS)
        stale_sids = []
        cutoff = _now() - HEARTBEAT_TIMEOUT_SECONDS
        with _lock:
            for sid, conn in list(_sid_to_conn.items()):
                if conn["last_seen_ts"] <= cutoff:
                    conn["disconnect_reason"] = "heartbeat_timeout"
                    stale_sids.append(sid)
            for sid in stale_sids:
                conn = _sid_to_conn.get(sid)
                if not conn:
                    continue
                try:
                    socketio.server.leave_room(sid, branch_room(conn["branch_id"]), namespace="/")
                except Exception:
                    pass
                _remove_sid_locked(sid)
        if stale_sids:
            record_error_event("stale_socket_cleanup", f"cleaned={len(stale_sids)}", "realtime")
            _schedule_force_disconnect(stale_sids, "heartbeat_timeout")


def ensure_realtime_background_tasks():
    global _cleanup_task_started
    with _lock:
        if _cleanup_task_started:
            return
        _cleanup_task_started = True
    socketio.start_background_task(_cleanup_stale_connections)


def get_active_device_counts() -> dict[int, int]:
    with _lock:
        summary = _rebuild_branch_summary_locked()
        return {branch_id: item["active_devices"] for branch_id, item in summary.items()}


def get_total_active_devices() -> int:
    with _lock:
        summary = _rebuild_branch_summary_locked()
        return sum(item["active_devices"] for item in summary.values())


def get_total_active_sockets() -> int:
    with _lock:
        return len(_sid_to_conn)


def get_branch_device_snapshot() -> list[dict]:
    with _lock:
        summary = _rebuild_branch_summary_locked()
    result = []
    for branch_id in sorted(summary):
        item = summary[branch_id]
        result.append({
            "branch_id": branch_id,
            "active_devices": item["active_devices"],
            "active_sockets": item["active_sockets"],
            "devices": item["devices"],
        })
    return result


def get_branch_active_devices(branch_id: int) -> dict:
    with _lock:
        summary = _rebuild_branch_summary_locked().get(branch_id, {
            "active_devices": 0,
            "active_sockets": 0,
            "devices": [],
        })
        return {
            "branch_id": branch_id,
            "active_devices": summary["active_devices"],
            "active_sockets": summary["active_sockets"],
            "devices": list(summary["devices"]),
        }


def _health_score(avg_ms: float, error_count: int, stale_count: int, active_sockets: int) -> int:
    score = 100
    score -= min(30, int(max(0, avg_ms - 120) / 15))
    score -= min(25, error_count * 4)
    score -= min(20, stale_count * 5)
    score -= min(10, max(0, active_sockets - 120) // 10)
    return max(0, min(100, score))


def get_health_snapshot() -> dict:
    now = _now()
    with _lock:
        for bucket in (
            _request_metrics,
            _scan_events,
            _error_events,
            _socket_connect_events,
            _socket_disconnect_events,
            _emit_events,
        ):
            _trim_deque(bucket)
        branch_summary = _rebuild_branch_summary_locked()
        avg_ms = (
            sum(item[1]["duration_ms"] for item in _request_metrics) / len(_request_metrics)
            if _request_metrics else 0
        )
        scans_last_window = len(_scan_events)
        errors_last_window = len(_error_events)
        stale_devices = sum(
            1
            for branch in branch_summary.values()
            for device in branch["devices"]
            if now - device["last_seen"] > 120
        )
        score = _health_score(avg_ms, errors_last_window, stale_devices, len(_sid_to_conn))
        return {
            "window_seconds": HEALTH_WINDOW_SECONDS,
            "server_uptime_seconds": int(now - _server_started_at),
            "active_devices": sum(item["active_devices"] for item in branch_summary.values()),
            "active_sockets": len(_sid_to_conn),
            "connected_branches": len(branch_summary),
            "requests_last_window": len(_request_metrics),
            "scans_last_window": scans_last_window,
            "errors_last_window": errors_last_window,
            "socket_connects_last_window": len(_socket_connect_events),
            "socket_disconnects_last_window": len(_socket_disconnect_events),
            "socket_emits_last_window": len(_emit_events),
            "avg_request_ms": round(avg_ms, 1),
            "stale_devices": stale_devices,
            "health_score": score,
            "health_status": (
                "critical" if score < 40 else
                "warning" if score < 70 else
                "good"
            ),
            "recent_errors": [
                {
                    "ts": item[1]["ts"],
                    "kind": item[1]["kind"],
                    "message": item[1]["message"],
                    "scope": item[1]["scope"],
                }
                for item in list(_error_events)[-10:]
            ],
            "branches": [
                {
                    "branch_id": branch_id,
                    "active_devices": item["active_devices"],
                    "active_sockets": item["active_sockets"],
                    "devices": item["devices"],
                }
                for branch_id, item in sorted(branch_summary.items())
            ],
        }


def record_request_metric(path: str, status_code: int, duration_ms: float):
    with _lock:
        _request_metrics.append((_now(), {
            "path": path,
            "status_code": status_code,
            "duration_ms": float(duration_ms),
        }))
        _trim_deque(_request_metrics)
    if "/scan" in path:
        record_scan_event(path)
    if status_code >= 500:
        record_error_event("http_5xx", f"{path} -> {status_code}", "http")


def record_scan_event(path: str):
    with _lock:
        _scan_events.append((_now(), {"path": path}))
        _trim_deque(_scan_events)


def record_error_event(kind: str, message: str, scope: str = "system"):
    ts = _now()
    with _lock:
        _error_events.append((ts, {
            "ts": int(ts),
            "kind": kind,
            "message": str(message),
            "scope": scope,
        }))
        while len(_error_events) > ERROR_BUFFER_LIMIT:
            _error_events.popleft()
        _trim_deque(_error_events)


def emit_update(branch_id: int, event: str, data: dict):
    room = branch_room(branch_id)
    with _lock:
        _emit_events.append((_now(), {"event": event, "branch_id": branch_id}))
        _trim_deque(_emit_events)
    socketio.emit(event, data, room=room)


def disconnect_branch_devices(branch_id: int, reason: str = "admin_disconnect_all") -> int:
    with _lock:
        sids = list(_branch_to_sids.get(branch_id, set()))
        for sid in sids:
            conn = _sid_to_conn.get(sid)
            if not conn:
                continue
            conn["disconnect_reason"] = reason
            try:
                socketio.server.leave_room(sid, branch_room(branch_id), namespace="/")
            except Exception:
                pass
            _remove_sid_locked(sid)
    for sid in sids:
        socketio.emit("force_logout", {"reason": reason}, to=sid)
    if sids:
        _schedule_force_disconnect(sids, reason)
    return len(sids)


def disconnect_single_device(branch_id: int, device_id: str, reason: str = "admin_disconnect_device") -> int:
    with _lock:
        sids = [
            sid for sid in list(_branch_to_sids.get(branch_id, set()))
            if _sid_to_conn.get(sid, {}).get("device_id") == device_id
        ]
        for sid in sids:
            conn = _sid_to_conn.get(sid)
            if not conn:
                continue
            conn["disconnect_reason"] = reason
            try:
                socketio.server.leave_room(sid, branch_room(branch_id), namespace="/")
            except Exception:
                pass
            _remove_sid_locked(sid)
    for sid in sids:
        socketio.emit("force_logout", {"reason": reason}, to=sid)
    if sids:
        _schedule_force_disconnect(sids, reason)
    return len(sids)


@socketio.on("join_branch")
def handle_join_branch(data):
    ensure_realtime_background_tasks()
    branch_id = data.get("branch_id")
    if not branch_id:
        return

    branch_id = int(branch_id)
    room = branch_room(branch_id)
    sid = request.sid
    device_id = str(data.get("device_id") or sid).strip()
    device_name = str(data.get("device_name") or "מכשיר ללא שם").strip() or "מכשיר ללא שם"
    now = _now()

    if _is_device_blocked(branch_id, device_id):
        emit("blocked_device", {"reason": "device_blocked", "device_id": device_id})
        socketio.start_background_task(lambda: socketio.server.disconnect(sid, namespace="/"))
        return

    with _lock:
        previous = _sid_to_conn.get(sid)
        if previous and previous["branch_id"] != branch_id:
            try:
                leave_room(branch_room(previous["branch_id"]))
            except Exception:
                pass
            _branch_to_sids[previous["branch_id"]].discard(sid)
            if not _branch_to_sids[previous["branch_id"]]:
                _branch_to_sids.pop(previous["branch_id"], None)

        _sid_to_conn[sid] = {
            "sid": sid,
            "branch_id": branch_id,
            "device_id": device_id,
            "device_name": device_name,
            "connected_at": previous["connected_at"] if previous else now,
            "last_seen": now,
            "last_seen_ts": now,
            "disconnect_reason": "disconnect",
        }
        _branch_to_sids[branch_id].add(sid)
        summary = _rebuild_branch_summary_locked().get(branch_id, {
            "active_devices": 0,
            "active_sockets": 0,
        })
        _socket_connect_events.append((_now(), {
            "sid": sid,
            "branch_id": branch_id,
            "device_id": device_id,
        }))
        _trim_deque(_socket_connect_events)

    join_room(room)
    emit("joined_branch", {
        "room": room,
        "active_devices": summary["active_devices"],
        "active_sockets": summary["active_sockets"],
        "device_id": device_id,
        "device_name": device_name,
    })


@socketio.on("heartbeat")
def handle_heartbeat(data):
    sid = request.sid
    now = _now()
    with _lock:
        conn = _sid_to_conn.get(sid)
        if not conn:
            return
        conn["last_seen"] = now
        conn["last_seen_ts"] = now
        device_name = str(data.get("device_name") or conn["device_name"]).strip() or conn["device_name"]
        conn["device_name"] = device_name
    emit("heartbeat_ack", {"ok": True})


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    sid = request.sid
    with _lock:
        conn = _sid_to_conn.get(sid)
        if conn and reason:
            conn["disconnect_reason"] = str(reason)
        _remove_sid_locked(sid)
