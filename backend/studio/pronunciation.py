"""Literal pronunciation aliases, applied once to a frozen generation input."""

import copy


def validate_rules(rules):
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError("Pronunciation rules must be a list of at most 200 entries")
    result, identifiers = [], set()
    allowed = {"id", "source", "replacement", "profile_id", "case_sensitive", "whole_word"}
    for value in rules:
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid pronunciation rule")
        rule = {"profile_id": None, "case_sensitive": False, "whole_word": True, **value}
        if (
            any(
                not isinstance(rule.get(key), str) or not rule[key].strip() or len(rule[key]) > limit
                for key, limit in (("id", 120), ("source", 200), ("replacement", 500))
            )
            or rule["id"] in identifiers
            or (
                rule["profile_id"] is not None
                and (
                    not isinstance(rule["profile_id"], str)
                    or not rule["profile_id"].strip()
                    or len(rule["profile_id"]) > 200
                )
            )
            or type(rule["case_sensitive"]) is not bool
            or type(rule["whole_word"]) is not bool
            or any(ch in rule["source"] or ch in rule["replacement"] for ch in ("\r", "\n", "\x00"))
        ):
            raise ValueError("Invalid pronunciation rule fields or duplicate ID")
        identifiers.add(rule["id"])
        result.append(rule)
    return result


def _word(character):
    return character.isalnum() or character == "_"


def spoken_text(text, rules, profile_id=None):
    """Leftmost-longest matches; equal-length voice rules precede project rules.

    Aliases are literal. Inserted replacements are not scanned again, and the
    source script remains the authoritative text for captions and provenance.
    """
    applicable = [r for r in validate_rules(rules) if r["profile_id"] is None or r["profile_id"] == profile_id]
    applicable.sort(key=lambda r: (-len(r["source"]), r["profile_id"] is None))
    output, index = [], 0
    while index < len(text):
        for rule in applicable:
            end = index + len(rule["source"])
            if end > len(text):
                continue
            actual = text[index:end]
            source = rule["source"]
            if (actual == source if rule["case_sensitive"] else actual.lower() == source.lower()) and (
                not rule["whole_word"]
                or ((index == 0 or not _word(text[index - 1])) and (end == len(text) or not _word(text[end])))
            ):
                output.append(rule["replacement"])
                index = end
                break
        else:
            output.append(text[index])
            index += 1
    return "".join(output)


def apply_pronunciation_command(doc, payload):
    if not isinstance(payload, dict) or set(payload) != {"rules"}:
        raise ValueError("Expected pronunciation rules")
    rules = validate_rules(payload["rules"])
    prior = doc.get("pronunciation_rules", [])
    affected = [
        p
        for p in doc["passages"]
        if spoken_text(p["text"], prior, p.get("profile_id")) != spoken_text(p["text"], rules, p.get("profile_id"))
    ]
    if any(p["locked"] for p in affected):
        raise ValueError(
            "A passage affected by pronunciation rules is locked; unlock it before changing its pronunciation"
        )
    for passage in affected:
        passage["input_revision"] += 1
        passage["selection_token"] += 1
    doc["pronunciation_rules"] = copy.deepcopy(rules)
