"""Preserve source-relative fade envelopes when timeline clips are split."""


def fade_envelopes(clip, length, source_frames):
    """Return (duration, offset) for each fade, rejecting malformed metadata."""
    result = []
    for direction in ("in", "out"):
        visible_key = f"fade_{direction}_samples"
        total_key = f"fade_{direction}_total_samples"
        offset_key = f"fade_{direction}_offset_samples"
        visible = clip.get(visible_key)
        if type(visible) is not int or visible < 0 or visible > length:
            raise ValueError("Invalid clip fade")
        if total_key not in clip and offset_key not in clip:
            result.append((visible, 0))
            continue
        total, offset = clip.get(total_key), clip.get(offset_key)
        if (
            type(total) is not int
            or type(offset) is not int
            or total <= 0
            or total > source_frames
            or offset < 0
            or offset >= total
            or visible != min(length, total - offset)
        ):
            raise ValueError("Invalid clip fade envelope")
        result.append((total, offset))
    return result


def set_segment_fade(clip, direction, length, total, offset):
    """Store the visible part and the original envelope for one segment."""
    visible_key = f"fade_{direction}_samples"
    total_key = f"fade_{direction}_total_samples"
    offset_key = f"fade_{direction}_offset_samples"
    clip.pop(total_key, None)
    clip.pop(offset_key, None)
    if not total or offset >= total:
        clip[visible_key] = 0
        return
    clip[visible_key] = min(length, total - offset)
    if offset or total > length:
        clip[total_key] = total
        clip[offset_key] = offset
