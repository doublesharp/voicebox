"""Validated, bounded Studio track effects using the existing effects registry."""

import math

MAX_EFFECTS = 16


def validate_chain(chain):
    if not isinstance(chain, list) or len(chain) > MAX_EFFECTS:
        raise ValueError("Invalid effect chain length")
    for effect in chain:
        if not isinstance(effect, dict) or set(effect) - {"type", "params", "enabled"}:
            raise ValueError("Invalid effect configuration")
        if not isinstance(effect.get("type"), str):
            raise ValueError("Invalid effect type")
        if type(effect.get("enabled", True)) is not bool:
            raise ValueError("Invalid effect enabled flag")
        if effect["type"] == "pitch_shift" and effect.get("enabled", True):
            raise ValueError("Pitch shifting is not supported by Studio's block renderer; disable it or use legacy Effects")
        params = effect.get("params", {})
        if not isinstance(params, dict) or any(
            not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v) for k, v in params.items()
        ):
            raise ValueError("Invalid effect parameters")
    if chain:
        from ..utils.effects import validate_effects_chain

        error = validate_effects_chain(chain)
        if error:
            raise ValueError(error)
    return chain


def make_board(chain):
    if not chain or not any(effect.get("enabled", True) for effect in chain):
        return None
    from ..utils.effects import build_pedalboard

    return build_pedalboard(chain)


def tail_frames(chain, rate):
    if not chain:
        return 0
    from ..utils.effects import _effect_tail_seconds

    return math.ceil(_effect_tail_seconds(chain) * rate)
