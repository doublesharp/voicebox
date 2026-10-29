"""Durable generation attempts and projection; safe to call from StudioStore._project."""

from .storage import now


def enrich_job(db, job_record):
    """Return an enriched copy of a projected job; render jobs retain their generic status."""
    return enrich_jobs(db, [job_record])[0]


def enrich_jobs(db, job_records):
    """Project attempt history in two queries for a whole page of jobs."""
    results = [dict(record) for record in job_records]
    if not results:
        return results
    attempts_by_job = {}
    attempts_by_key = {}
    # SQLite installations may retain the older 999-variable limit.
    ids = [result["id"] for result in results]
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in db.execute(
            f"SELECT * FROM job_attempts WHERE job_id IN ({placeholders}) ORDER BY job_id,attempt", batch
        ):
            item = dict(row)
            job_id = item.pop("job_id")
            item["phases"] = []
            attempts_by_job.setdefault(job_id, []).append(item)
            attempts_by_key[job_id, item["attempt"]] = item
        for row in db.execute(
            f"SELECT job_id,attempt,phase,created_at FROM job_phase_events WHERE job_id IN ({placeholders}) ORDER BY id",
            batch,
        ):
            attempt = attempts_by_key.get((row["job_id"], row["attempt"]))
            if attempt is not None:
                attempt["phases"].append({"phase": row["phase"], "created_at": row["created_at"]})
    for result in results:
        attempts = attempts_by_job.get(result["id"], [])
        result["attempts"] = attempts
        result["phase"] = (
            result["status"] if result["status"] != "running" else attempts[-1]["phase"] if attempts else "running"
        )
    return results


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
