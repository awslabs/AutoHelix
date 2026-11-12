Goal: {{ goal }}

Your working directory is {{ worktree }}. Don't modify files outside it unless
the goal directs you to.

This is iteration {{ iteration }}. Baseline is iter-0.

{% if history_summary %}
Recent history:
{{ history_summary }}
{% endif %}

{% if constraints %}
Constraints (must pass — failure means your changes are discarded):
{% for c in constraints %}
  {{ c }}
{% endfor %}
{% endif %}

{% if observables %}
Metrics (measured after you finish):
{% for obs in observables %}
  {{ obs.command }}{% if obs.metric_labels %} → {{ obs.metric_labels | join(', ') }}{% endif +%}
{% endfor %}
{% endif %}

{% if stop %}
Stop conditions (the run ends once one of these exits 0 after an accepted iteration):
{% for c in stop %}
  {{ c }}
{% endfor %}
{% endif %}

{% if editable %}
Editable (only changes to these files will be kept): {{ editable | join(', ') }}
{% elif frozen %}
Frozen (changes to these files will be reverted): {{ frozen | join(', ') }}
{% endif %}

Read before starting:
{% if has_reviewer %}
- .autohelix-iteration/review.md
{% endif %}
- .autohelix-iteration/notes/iter-*.md — your own notes from past iterations
{% if observables %}
- .autohelix-iteration/observations/iter-*/ — captured metric outputs, one dir per iteration
{% endif %}
{% if has_hints %}
- .autohelix-iteration/hints.md
{% endif %}

{% if group_runs %}
Other runs in group `{{ group }}` (copies of their state as of this iteration's start, read-only):
{% for line in group_runs %}
- {{ line }}
{% endfor %}
Each has run.json, history.jsonl and notes/. Their accepted code is on the refs above, in this
copy's git: `git diff autohelix/<id>`, `git show autohelix/<id>:<file>`,
`git checkout autohelix/<id> -- <file>`. Build on it or explore a different approach. If you
adopt code from another run, name that run in your summary (e.g. "adopted the parser from k3f9").
{% endif %}

{% if iteration_time %}
Time budget: {{ iteration_time }}. The process will be killed at the deadline.
To check remaining time: bash "$AUTOHELIX_TIME_LEFT_SCRIPT"
Stop new experiments before the deadline, write notes, and exit cleanly.
{% endif %}

You MUST write notes when done — write to {{ worktree }}/.autohelix-iteration/notes/iter-{{ iteration }}.md.
Record what you tried, what worked or failed, and what to try next.
Notes persist even if your changes are rejected, so they are the only way to pass knowledge to future iterations.

Make changes to improve toward the goal.
Do not create git commits; AutoHelix commits accepted changes for you.
When done, write a one-line summary of what you changed and why to {{ worktree }}/.autohelix-iteration/commit_summary.txt
