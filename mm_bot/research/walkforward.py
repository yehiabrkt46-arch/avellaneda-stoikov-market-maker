# mm_bot/research/walkforward.py
"""Chronological train/test split for walk-forward parameter selection.

Shuffled cross-validation leaks information across time: a parameter set
that happens to fit a later day's regime could be picked using data that,
in live trading, would not exist yet at selection time. Splitting on the
calendar day index instead, earliest days train, latest days test, matches
how the parameter sweep is actually meant to be used: pick a config from
the past, then measure it on days it never saw.
"""
import math


def train_test_split_days(day_buckets: list[int], train_frac: float = 0.7) -> tuple[list[int], list[int]]:
    """Chronological (not shuffled) walk-forward split: earliest days are train, latest are test."""
    unique_days = sorted(set(day_buckets))
    if len(unique_days) < 2:
        # too little history to hold out a test day at all; keep everything
        # in train and leave test empty rather than raising.
        return unique_days, []
    train_count = math.ceil(len(unique_days) * train_frac)
    return unique_days[:train_count], unique_days[train_count:]
