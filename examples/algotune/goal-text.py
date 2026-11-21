"""The /goal arm's text: the config's goal without what only AutoHelix does.

    python -I goal-text.py autohelix/algotune/config.yaml
"""
import sys

import yaml

goal = yaml.safe_load(open(sys.argv[1]))["goal"]
start = goal.index("Check your work with")
print(goal[:start] + (
    "Check your work with `sudo -n algotune-gate --limit N` (the first N development\n"
    "instances) or `sudo -n algotune-gate` (all of them, as the final development check;\n"
    "its output ends with the time it took). Run one gate at a time (another waits for\n"
    "it); `sudo -n algotune-gate --stop` ends one you started.\n"))
