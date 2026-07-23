# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Parallel exploration (alpha): run N AutoHelix workers concurrently.

This is a self-contained, more experimental layer on top of the core loop.
Nothing in core imports it; only the `autohelix parallel` CLI command reaches
in, via the public entry points re-exported here. Implementation lives in
`core.py` (orchestration) and `display.py` (live Rich display).
"""

from autohelix.parallel.core import leaderboard, run_parallel

__all__ = ["leaderboard", "run_parallel"]
