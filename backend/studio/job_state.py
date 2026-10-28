"""Durable generation attempts and projection; safe to call from StudioStore._project."""

from .storage import now


def enrich_job(db, job_record):
    """Return an enriched copy of a projected job; render jobs retain their generic status."""
    result = dict(job_record)
    attempts = [
        dict(row)
        for row in db.execute(
            "SELECT attempt,phase,started_at,updated_at,error FROM job_attempts WHERE job_id=? ORDER BY attempt",
            (result["id"],),
        )
    ]
    for item in attempts:
        item["phases"] = [
            dict(row)
            for row in db.execute(
                "SELECT phase,created_at FROM job_phase_events WHERE job_id=? AND attempt=? ORDER BY id",
                (result["id"], item["attempt"]),
            )
        ]
    result["attempts"] = attempts
    result["phase"] = (
        result["status"] if result["status"] != "running" else attempts[-1]["phase"] if attempts else "running"
    )
    return result


def start_attempt(db, job_id):
    attempt = db.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()[0]
    stamp = now()
    db.execute("INSERT INTO job_attempts VALUES (?,?,?,?,?,NULL)", (job_id, attempt, "running", stamp, stamp))
    db.execute(
        "INSERT INTO job_phase_events (job_id,attempt,phase,created_at) VALUES (?,?,?,?)",
        (job_id, attempt, "running", stamp),
    )
    return attempt


def phase(db, job_id, attempt, value, error=None):
    stamp = now()
    db.execute(
        "UPDATE job_attempts SET phase=?,updated_at=?,error=? WHERE job_id=? AND attempt=?",
        (value, stamp, error, job_id, attempt),
    )
    db.execute(
        "INSERT INTO job_phase_events (job_id,attempt,phase,created_at) VALUES (?,?,?,?)",
        (job_id, attempt, value, stamp),
    )
