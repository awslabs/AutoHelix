"""Score a bin-packing solution. FROZEN — the agent must not edit this.

Loads the instance and the agent's submitted packing (solution.json), validates
it, and prints the metric AutoHelix ranks on.

Usage:
    python score.py            # validate + print ##autohelix[bins=N]
    python score.py --check    # validate only; exit non-zero if infeasible

`--check` is the constraint (a hard feasibility gate): if the packing is invalid,
the iteration is rejected and changes are discarded. The default mode is the
metric: `bins` (lower is better).

The instance is fixed, so there's no solver to run — the agent finds a good
packing however it likes (a script in its worktree, by hand, whatever) and writes
the result to solution.json. This scorer only checks that the submitted packing
is legal and counts its bins; it can't be gamed, because the only way to score
lower is to actually place the items into fewer bins.
"""

import json
import sys
from pathlib import Path

CAPACITY_KEY = "capacity"
HERE = Path(__file__).parent


def _load_instance() -> tuple[list[int], int]:
    data = json.loads((HERE / "instance.json").read_text())
    return data["sizes"], data[CAPACITY_KEY]


def _load_solution() -> list[list[int]]:
    path = HERE / "solution.json"
    if not path.exists():
        raise SystemExit("solution.json not found — write the packing there")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"solution.json is not valid JSON: {e}")
    if not isinstance(data, dict) or "bins" not in data:
        raise SystemExit('solution.json must be an object with a "bins" key')
    return data["bins"]


def _validate(bins, sizes, capacity) -> int:
    """Return bin count if valid; raise SystemExit with a clear reason otherwise."""
    if not isinstance(bins, list):
        raise SystemExit('"bins" must be a list of bins (each a list of item indices)')
    n = len(sizes)
    seen: list[int] = []
    for b_idx, b in enumerate(bins):
        if not isinstance(b, list):
            raise SystemExit(f"bin {b_idx} is not a list of item indices")
        total = 0
        for i in b:
            if not isinstance(i, int) or i < 0 or i >= n:
                raise SystemExit(f"bin {b_idx} has invalid item index {i!r}")
            total += sizes[i]
            seen.append(i)
        if total > capacity:
            raise SystemExit(f"bin {b_idx} over capacity: {total} > {capacity}")
    if sorted(seen) != list(range(n)):
        dupes = len(seen) - len(set(seen))
        missing = n - len(set(seen))
        raise SystemExit(
            f"packing must place each item exactly once "
            f"({dupes} duplicated, {missing} missing of {n})"
        )
    return len(bins)


def main() -> None:
    check_only = "--check" in sys.argv
    sizes, capacity = _load_instance()
    bins = _load_solution()
    num_bins = _validate(bins, sizes, capacity)

    lower_bound = -(-sum(sizes) // capacity)  # ceil(total / capacity)
    if check_only:
        print(f"OK: valid packing, {num_bins} bins (theoretical min {lower_bound})")
        return
    print(f"bins={num_bins}  lower_bound={lower_bound}  items={len(sizes)}")
    print(f"##autohelix[bins={num_bins}]")


if __name__ == "__main__":
    main()
