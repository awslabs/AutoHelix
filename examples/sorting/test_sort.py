"""Tests for sort module."""

from sort import sort_list


def test_empty_list():
    assert sort_list([]) == []


def test_single_element():
    assert sort_list([1]) == [1]


def test_already_sorted():
    assert sort_list([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert sort_list([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_random_order():
    assert sort_list([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_duplicates():
    assert sort_list([3, 3, 3, 1, 1]) == [1, 1, 3, 3, 3]


def test_negative_numbers():
    assert sort_list([-3, 1, -4, 1, 5]) == [-4, -3, 1, 1, 5]


def test_large_list():
    import random
    random.seed(42)
    arr = [random.randint(0, 1000) for _ in range(100)]
    result = sort_list(arr)
    assert result == sorted(arr)


def test_does_not_mutate_input():
    arr = [3, 1, 4, 1, 5, 9, 2, 6]
    original = arr.copy()
    sort_list(arr)
    assert arr == original, "sort_list must not modify its input"
