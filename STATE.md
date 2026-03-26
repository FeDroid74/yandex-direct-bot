# STATE.md — Campaign State

You are building a structured campaign object.

Store all collected data here:

campaign:
  campaign_name:
  site_url:
  region:
  language: RU
  placement_type:
  goal_type:
  strategy_type:
  metrica_goal_id:
  daily_budget:
  schedule:
  utm_tracking: true

  product:
  audience:
  value:

  ad_groups: []
  ads: []

---

## Rules

- Always remember previous answers
- Never ask the same question twice
- Fill missing fields step-by-step
- Do not proceed if critical fields are missing
- Use backend validation as the source of truth before create

---

## Behavior

You are filling this structure gradually.

Each user answer updates STATE.

Before next question:
- check what is already filled
- ask ONLY for one missing field
- if full payload is already provided, call validate_campaign first
- do not invent validation failures manually
- validate critical campaign fields through backend before create
- never call create_campaign without explicit user confirmation
