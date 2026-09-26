# Product campaign implementation

Scope: ArtFarfor only. Existing working campaigns remain unchanged until the
owner approves each independent production action in Telegram.

## Non-negotiable constraints

- Only PAY_FOR_CONVERSION for purchase goal 352606262, counter 99041859.
- No click billing, cart goal, multi-goal or portfolio fallback.
- A replacement is a new Unified Campaign with ShoppingAd, not an in-place
  conversion of existing ads. Creation, launch and stopping the old campaign
  are separate owner decisions.
- Feed availability is an inventory fact, not proof of a paid sale.
- Prefer the store's native, automatically refreshed YML URL. Do not invent
  inventory, prices, categories, feed URLs or campaign performance.
- No extra LLM schedules. Include replacement review in the existing weekly
  marketer. Feed refresh is deterministic, not a model task.
- No automatic retry of a possibly completed Direct write. Persist individual
  creation steps, retain partial entity IDs and require reconciliation.

## Steps

1. [x] Back up production code/configuration and SQLite before changing files.
2. [x] Read current integration and official Feeds/ShoppingAd API contracts.
3. [ ] Full sandbox flow blocked: feed accepted, campaigns.add returns HTTP 500 NotImplemented. Do not substitute an unapproved production test.
4. [x] Implement validated catalog inspection and revisioned product drafts.
5. [x] Implement owner-approved creation, editing and lifecycle operations.
6. [x] Add evidence-based replacement candidates and weekly analyst context.
7. [x] Test invariants, stale approvals, partial writes and plugin integration (91 Python tests and native plugin tests).
8. [x] Deploy and verify read-only production integration.

Backup: /home/appuser/backups/product-campaigns-20260926T130037Z
Baseline commit: 3c0972889306f3de5894dc011473b9390290d39f

Native YML URL recovered from the supplied file's Windows download metadata and
verified without authentication from VPS. The InSales URL redirects to the store's
/marketplace/4675993.xml?download=true endpoint. Existing site-generated and old
FILE feeds must not be mistaken for this current inventory source.

Owner supplied 4675993.xml, generated 2026-09-26T16:08:13+03:00. Structural
validation: 595 offers, 20 categories, all explicitly available, RUB prices,
no invalid entries. Live URL returned a newer generation date and passed the same
validation. Correct stock/warehouse configuration still needs owner verification.
Feed registration card c5a737968f76 v1 was delivered to Telegram, pending approval.

VPS staging verification: 91 Python tests and plugin tests passed. Live checks:
marketer health OK; Direct feed list read (18 entries); existing campaign/ads
inspection OK; all 13 plugin tools loaded; gateway and Telegram health OK.
Weekly run for September 21 completed; timer remains active for September 28.
No production advertising objects were created, updated, moderated or stopped.

## Primary API references

- https://yandex.ru/dev/direct/doc/ru/feeds/feeds
- https://yandex.ru/dev/direct/doc/ru/feeds/add
- https://yandex.ru/dev/direct/doc/ru/feeds/get
- https://yandex.ru/dev/direct/doc/ru/ads/add
- https://yandex.ru/dev/direct/doc/ru/ads/update
- https://yandex.ru/dev/direct/doc/ru/objects/ad
- https://yandex.ru/dev/direct/doc/ru/adgroups/add
- https://yandex.ru/support/direct/ru/feeds/requirements-yml
