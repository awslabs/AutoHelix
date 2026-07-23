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

{% if editable %}
Editable (only changes to these files will be kept): {{ editable | join(', ') }}
{% elif frozen %}
Frozen (changes to these files will be reverted): {{ frozen | join(', ') }}
{% endif %}

Read before starting:
{% if has_reviewer %}
- .autohelix/review.md
{% endif %}
- .autohelix/notes/iter-*.md — your own notes from past iterations
{% if observables %}
- .autohelix/observations/iter-*/ — captured metric outputs, one dir per iteration
{% endif %}
{% if peers %}
- .autohelix/peer_notes/*/iter-*.md — what parallel peers have tried (one dir per peer)
{% endif %}
{% if has_hints %}
- .autohelix/hints.md
{% endif %}

{% if peers %}
To see a peer's accepted code (not just their notes): `git show parallel/<peer>:<file>` (peers: {{ peers | join(', ') }}). Build on it or explore a different approach.
{% endif %}

{% if iteration_time %}
Time budget: {{ iteration_time }}. The process will be killed at the deadline.
To check remaining time: bash "$AUTOHELIX_TIME_LEFT_SCRIPT"
Stop new experiments before the deadline, write notes, and exit cleanly.
{% endif %}

You MUST write notes when done — write to {{ worktree }}/.autohelix/notes/iter-{{ iteration }}.md.
Record what you tried, what worked or failed, and what to try next.
Notes persist even if your changes are rejected, so they are the only way to pass knowledge to future iterations.

Make changes to improve toward the goal.
When done, write a one-line summary of what you changed and why to {{ worktree }}/.autohelix/commit_summary.txt
