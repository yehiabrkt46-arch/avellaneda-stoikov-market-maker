# tests/test_walkforward.py
from mm_bot.research.walkforward import train_test_split_days


def test_split_is_chronological_not_shuffled():
    days = [5, 1, 3, 4, 2]  # unordered on purpose
    train, test = train_test_split_days(days, train_frac=0.6)
    assert train == [1, 2, 3]
    assert test == [4, 5]


def test_split_respects_train_frac():
    days = list(range(10))
    train, test = train_test_split_days(days, train_frac=0.7)
    assert train == [0, 1, 2, 3, 4, 5, 6]
    assert test == [7, 8, 9]


def test_split_deduplicates_days():
    days = [1, 1, 2, 2, 3, 3, 4]
    train, test = train_test_split_days(days, train_frac=0.5)
    assert train == [1, 2]
    assert test == [3, 4]


def test_default_train_frac_is_seventy_percent():
    days = list(range(4))
    train, test = train_test_split_days(days)
    assert train == [0, 1, 2]
    assert test == [3]


def test_fewer_than_two_days_puts_everything_in_train():
    assert train_test_split_days([]) == ([], [])
    assert train_test_split_days([7]) == ([7], [])
    assert train_test_split_days([7, 7, 7]) == ([7], [])
