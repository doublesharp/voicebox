"""Pure portable checkpoint-review validation. No store or database dependencies."""

import copy


def validate_review_content(note, checklist):
    if not isinstance(note, str) or len(note) > 4000:
        raise ValueError("Review note must be text up to 4000 characters")
    if not isinstance(checklist, list) or len(checklist) > 12:
        raise ValueError("Review checklist must have at most 12 entries")
    ids = set()
    for item in checklist:
        if not isinstance(item, dict) or set(item) != {"id", "label", "checked"}:
            raise ValueError("Invalid review checklist entry")
        if (
            not isinstance(item["id"], str)
            or not 1 <= len(item["id"]) <= 80
            or item["id"] in ids
            or not isinstance(item["label"], str)
            or not 1 <= len(item["label"]) <= 120
            or type(item["checked"]) is not bool
        ):
            raise ValueError("Invalid review checklist values")
        ids.add(item["id"])


def validate_reviews(project):
    """Validate optional project.reviews; absent means a legacy archive with no reviews."""
    records = project.get("reviews", [])
    if not isinstance(records, list):
        raise ValueError("Reviews must be an array")
    revisions = {r["id"] for r in project.get("revisions", [])}
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"revision_id", "version", "note", "checklist", "updated_at"}:
            raise ValueError("Invalid review record")
        revision = record["revision_id"]
        if not isinstance(revision, str) or revision not in revisions or revision in seen:
            raise ValueError("Review revision missing or duplicated")
        if type(record["version"]) is not int or record["version"] < 1:
            raise ValueError("Invalid review version")
        if not isinstance(record["updated_at"], str) or not record["updated_at"].strip():
            raise ValueError("Invalid review timestamp")
        validate_review_content(record["note"], record["checklist"])
        seen.add(revision)
    return records


def remap_reviews(project, mapping):
    records = copy.deepcopy(validate_reviews(project))
    for record in records:
        record["revision_id"] = mapping[record["revision_id"]]
    return records


def approval_note(review):
    """Compatible immutable, human-readable snapshot in the existing local approval note."""
    lines = [review["note"], f"Local review snapshot · version {review['version']}"]
    lines.extend(f"[{'x' if item['checked'] else ' '}] {item['label']}" for item in review["checklist"])
    return "\n".join(lines)
