Goal: {{ goal }}

Your working directory is {{ worktree }}. Don't modify files outside it unless
the goal directs you to.

This is iteration {{ iteration }}. Baseline is iter-0.

{% if history %}
Recent history:
{{ history }}
{% endif %}

{% if last %}
{% if last.accepted %}
Last accepted: {{ "the baseline (iter-0)" if last.iteration == 0 else "iter-" ~ last.iteration }}; your copy holds its code.
{% else %}
Iteration {{ last.iteration }} was rejected: {{ last.reason }}
Your copy holds the last accepted code.
{% endif %}
{% if last.gates %}
{{ "Its" if last.accepted else "Its rejected attempt's" }} gate results:
{% for g in last.gates %}
  {{ {"passed": "✓", "failed": "✗", "pending": "·"}[g.status] }} {{ g.text }}{% if g.status == "pending" %} (not reached){% endif +%}
{% endfor %}
{% endif %}
{% if not last.accepted and last.failure_output %}
Failure output:
{{ last.failure_output | indent(4, true) }}
{% endif %}
{% if last.observe %}
Read the latest observations ({{ "its" if last.accepted else "what the rejected attempt produced" }}):
{% for o in last.observe %}
  {{ o.name }}: {{ o.path }}{% if not o.ok %} (failed: {{ o.error }}){% endif +%}
{% endfor %}
{% endif %}
{% if rejected_patch %}
The previous attempt was rejected{% if last.failure_output %} (failure output above){% endif %}. Its changes are in `{{ rejected_patch }}`; `git apply` it if it was close, or start over.
{% elif rejected_diff %}
The previous attempt was rejected{% if last.failure_output %} (failure output above){% endif %}. Its changes are too large for a patch file; see them with `{{ rejected_diff }}` (`| git apply` them if it was close), or start over.
{% endif %}
{% endif %}
{% if discarded %}
Your changes to {{ discarded | join(', ') }} were discarded: outside the editable scope.
{% endif %}

{% if gates %}
Gates (must pass — failure means your changes are discarded):
{% for g in gates %}
  {{ g.text }}
{% endfor %}
{% endif %}

{% if observe %}
Observed after you finish:
{% for o in observe %}
  {{ o.name }}: {{ o.command if o.kind == "command" else "review: " ~ o.prompt }}{% if o.metric_labels %} → {{ o.metric_labels | join(', ') }}{% endif +%}
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
{% endif %}
{% if frozen %}
Frozen (changes to these files will be reverted{% if editable %}, even inside the editable ones{% endif %}): {{ frozen | join(', ') }}
{% endif %}

Read before starting:
- .autohelix-iteration/notes/iter-*.md — your own notes from past iterations
{% if observe %}
- .autohelix-iteration/observations/iter-*/<name>/ — each observe entry's stdout.txt, stderr.txt and captured files
{% endif %}
{% if hints %}
- {{ hints }}
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

{% if time_limit %}
Time limit: {{ time_limit }}. The process will be killed at the deadline.
To check remaining time: bash "$AUTOHELIX_TIME_LEFT_SCRIPT"
Stop new experiments before the deadline, write notes, and exit cleanly.
{% endif %}

You MUST write notes when done — write to {{ notes_path }}.
Record what you tried, what worked or failed, and what to try next.
Notes persist even if your changes are rejected, so they are the only way to pass knowledge to future iterations.

Make changes to improve toward the goal.
Do not create git commits; AutoHelix commits accepted changes for you.
When done, write a one-line summary of what you changed and why to {{ worktree }}/.autohelix-iteration/commit_summary.txt
