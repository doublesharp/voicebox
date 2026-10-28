"""Nonnegative project times round to nearest sample, ties toward +infinity.

This matches JavaScript Math.round used by the studio timeline.
"""

import math


def nearest_sample(value):
    return math.floor(value + 0.5)


def resampled_frames(frames, source_rate, working_rate):
    return (2 * frames * working_rate + source_rate) // (2 * source_rate)
