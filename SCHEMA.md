# SCHEMA.md — Yandex Direct v1

## CAMPAIGN (обязательно для backend validate/create)

- campaign_name
- site_url (https://artfarfor.com only)
- region
- language = RU
- placement_type (both only)
- goal_type (leads / sales)
- strategy_type = pay_for_conversion
- metrica_goal_id
- weekly_budget_rub
- target_cpa_rub

---

## CAMPAIGN (необязательно в текущем backend, но желательно для дальнейшей сборки)

- start_date
- schedule
- utm_tracking (enabled)
- ad_groups
- ads

---

## AD GROUP (структура для следующего этапа)

- group_name
- geo
- keywords OR autotargeting
- negative_keywords

---

## AD (структура для следующего этапа)

- title
- text
- final_url
- images (optional but recommended)
- sitelinks (optional)
- callouts (optional)

---

## PRE-FLIGHT CHECK

Перед созданием:

MUST HAVE:
- campaign_name
- site_url
- region
- language
- placement_type
- goal_type
- strategy_type = pay_for_conversion
- metrica_goal_id
- weekly_budget_rub
- target_cpa_rub

VALIDATION SOURCE OF TRUTH:
- backend /validate_campaign

IF backend validation fails:
→ STOP
→ show backend errors
→ ask only for the first blocking missing/invalid field

IF backend validation passes:
→ report success
→ do not create campaign without explicit confirmation

---

## AUTONOMY RULES

AUTO:
- add negative keywords
- send reports

CONFIRM REQUIRED:
- create campaign
- launch campaign
- change budget
- change strategy
- change texts
- add groups
- pause ads

---

## OPTIMIZATION RULES

Cycle: 7 days

Analyze:
- CPA
- conversions
- CTR
- CPC

Actions:
- suggest improvements
- propose new texts
- propose negatives
- propose structure changes

DO NOT APPLY without confirmation (except negatives)

---

## RESTRICTIONS

- ONLY artfarfor.com
- ONLY UPC
- ONLY RU language
- ONLY pay_for_conversion strategy
- ONLY weekly budget
- NO daily_budget
- NO assumptions as facts
- ALL assumptions must be labeled
