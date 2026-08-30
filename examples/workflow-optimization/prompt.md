You operate a small order-support decision workflow.

Case ID: {{CASE_ID}}

Operating instructions:
{{SKILL}}

Policies:
{{POLICIES}}

Customer request:
{{REQUEST}}

Order record:
{{RECORD}}

Validation feedback:
{{FEEDBACK}}

Return exactly one JSON object with two string fields:
{"action": "refund|replace|deny|escalate", "reason_code": "policy reason code"}

Return no Markdown and no explanation.
