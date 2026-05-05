"""WebSocket endpoint for job progress updates.

Wire protocol:
- Client opens /ws/{job_id}?since_seq=<int>
- Server sends snapshot, replays events, then streams live events
- Heartbeat ping every 20s
- Terminal states close the socket cleanly
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.session.manager import session_manager
from backend.session.progress import build_snapshot_event

router = APIRouter()

HEARTBEAT_SECONDS = 20.0
TERMINAL_STATUSES = {"complete", "error", "cancelled", "awaiting_confirmation"}


@router.websocket("/ws/{job_id}")
async def job_updates(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()

    since_seq_raw = websocket.query_params.get("since_seq", "0")
    try:
        since_seq = max(0, int(since_seq_raw))
    except (TypeError, ValueError):
        since_seq = 0

    queue = session_manager.subscribe_ws(job_id)
    try:
        job = session_manager.get_job(job_id)
        if job is None:
            await websocket.send_json({
                "type": "error",
                "job_id": job_id,
                "stage": "error",
                "status": "error",
                "message": "Job not found.",
                "progress": 0.0,
                "data": {"error": "not_found"},
            })
            await websocket.close(code=1008)
            return

        # 1. Send snapshot
        snapshot = build_snapshot_event(job_id, job)
        snapshot["last_seq"] = job.last_seq
        await websocket.send_json(snapshot)

        # 2. Replay missed events
        replayed_seqs: set[int] = set()
        for event in session_manager.get_events_after(job_id, since_seq):
            seq = int(event.get("seq", 0) or 0)
            if seq:
                replayed_seqs.add(seq)
            await websocket.send_json(event)

        # If terminal, drain and close
        job = session_manager.get_job(job_id)
        if job and job.status in TERMINAL_STATUSES and not session_manager.is_job_running(job_id):
            await _drain_queue_into_socket(websocket, queue, replayed_seqs)
            await websocket.close(code=1000)
            return

        # 3. Stream live events with heartbeats
        while True:
            event = await _next_event_or_heartbeat(websocket, queue)
            if event is None:
                continue
            seq = int(event.get("seq", 0) or 0)
            if seq and seq in replayed_seqs:
                continue
            await websocket.send_json(event)

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        session_manager.unsubscribe_ws(job_id, queue)


async def _next_event_or_heartbeat(
    websocket: WebSocket,
    queue: asyncio.Queue,
) -> dict | None:
    try:
        return await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "ping", "ts": _now()})
        return None


async def _drain_queue_into_socket(
    websocket: WebSocket,
    queue: asyncio.Queue,
    already_sent: set[int],
) -> None:
    while not queue.empty():
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        seq = int(event.get("seq", 0) or 0)
        if seq and seq in already_sent:
            continue
        try:
            await websocket.send_json(event)
        except Exception:
            return


def _now() -> float:
    import time
    return time.time()
