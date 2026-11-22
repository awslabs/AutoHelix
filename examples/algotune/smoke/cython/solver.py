"""Smoke test of the evaluator's build path, for the task eigenvalues_real: a Cython module
(fast.pyx) that setup.py builds in place before the evaluation. In the agent container,
with the task eigenvalues_real, as `agent`:

    cp -r examples/algotune/smoke/cython /tmp/smoke && cd /tmp/smoke
    sudo -n algotune-gate --limit 2     # builds fast.pyx, then scores it (about 1.0x)
"""

from fast import descending_eigenvalues


class Solver:
    def solve(self, problem, **kwargs):
        return descending_eigenvalues(problem)
