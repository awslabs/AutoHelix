# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AutoHelix - Minimal harness for autonomous code improvement agents."""

from autohelix.config import Config, ConfigIssue, load_config
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.terminal import version as _version

__version__ = _version()  # the installed package's (pyproject.toml), never written twice
__all__ = ["Config", "ConfigIssue", "load_config", "Harness", "History"]
