# cython: language_level=3
import numpy as np


def descending_eigenvalues(problem):
    cdef double[::1] values = np.linalg.eigvalsh(np.asarray(problem, dtype=np.float64))
    cdef Py_ssize_t i, n = values.shape[0]
    return [values[n - 1 - i] for i in range(n)]
