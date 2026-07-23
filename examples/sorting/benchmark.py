"""Benchmark for sort performance."""

import random
import time

from sort import sort_list


def benchmark(n_arrays: int = 50, size: int = 1000) -> float:
    """Sort many distinct random arrays and return sorts per second.

    A fresh unseeded RNG generates a different set of arrays every run, so the
    speed can't be gamed by caching or precomputing results — the only way to
    score higher is to sort faster.
    """
    rng = random.Random()  # unseeded: new inputs each run, can't be precomputed
    arrays = [[rng.randint(0, 10000) for _ in range(size)] for _ in range(n_arrays)]

    start = time.perf_counter()
    for arr in arrays:
        sort_list(arr)
    elapsed = time.perf_counter() - start

    return n_arrays / elapsed


if __name__ == "__main__":
    speed = benchmark()
    print(f"##autohelix[speed={speed:.2f}]")
