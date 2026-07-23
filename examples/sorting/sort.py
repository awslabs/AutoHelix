"""Sorting implementation."""


def sort_list(arr: list) -> list:
    """Sort a list of numbers."""
    n = len(arr)
    arr = arr.copy()
    for i in range(n):
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return arr
