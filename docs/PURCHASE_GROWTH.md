# Monthly purchase objective

Owner instruction, 2026-11-13: make monthly growth in purchases from advertising
the marketer's principal objective. Growth is an optimization goal, not a sales
guarantee, an attribution claim, or permission to expand spending/autonomy.
The next assessable month's numeric orientation is at least one purchase reach
above the latest mature month's total; it is not a forecast or an added budget.

## Measurement

`purchase_growth` in the frozen analysis snapshot compares three complete
calendar months whose final day precedes the configured conversion maturity
cutoff. It also compares equal elapsed mature days in the current and previous
month, explicitly as a provisional signal. Report time zone must match the
counter. Later conversions can revise an earlier observation.

Primary signal: unsampled Metrika reaches for Ecommerce purchase goal 352606262
in counter 99041859, filtered by a single enumerated campaign population under
the existing lastsign attribution. Inventory includes OFF and archived campaigns
to avoid selecting only today's survivors. Configured campaign scope is respected;
missing or oversized inventory fails closed. Site-wide purchases are separate
supporting context, never attributed to the bot or added to ad purchases.
Neither goal reaches nor Direct conversions prove payment, unique orders,
profit, absence of refunds, or an effect caused by the bot. No CRM API is required.

Unknown totals, sampled responses, an unverified purchase goal, or mismatched
counter time zone cannot become zero. Flat/decreased totals require diagnosis;
growth from zero has no percentage. Calendar-month length, seasonality, small
samples, attribution and assortment changes limit interpretation.
Monthly reads request only this additive goal metric. If the unsampled query is
too complex, disjoint date partitions are combined; a failed/sampled partition
invalidates the entire monthly total, never a zero-fill or lower accuracy.

## Existing cycle

No new timers: the Monday account review and explicit read-only refresh collect
the calendar report. Light optimization slots reuse the previous report with its
date. A new/revised monthly observation can open the existing analysis data gate,
even when rolling traffic contains little new data. Unchanged observations do not
force new model calls. Saved snapshots retain previous observations.

The analyst must return a source-matching `growth_review.status`, a diagnosis,
one prioritized next step and optional verified excerpt IDs. It must distinguish
observations from causal hypotheses, consider actual experiment history and
identify a missing fact when evidence cannot support an intervention. Courses
are untrusted reference material; citations retain lesson/locator. The merge
retains this review and may select only previously validated candidate actions.

Text-only ShoppingAd edits are observed after 28 mature days using the existing
experiment evaluator, including baseline settings, feed/filter/text drift and
other-change confounders. Sparse/unknown purchase data remains inconclusive.
Product setup, filters and other operational-only actions are not sales tests.
No autonomous rollback and no claim of statistically proven improvement.

`/yd growth` reads the last collected monthly status without invoking a model.
`marketer_review` and `/yd review` expose the same evidence. `/yd review fresh`
collects and delivers a read-only account review. Requested reviews separate
proposed, backend-rejected, pending, applied and uncertain outcomes.

## Unchanged controls

Only the existing bounded scheduled executor can apply eligible negatives and
TextAd changes. Purchase-only billing, weekly/campaign cooldowns, independent
review and uncertain-write stops are unchanged. ShoppingAd, channel, feed,
budget, CPA, launch and pause changes still need distinct real owner cards.
An interactive read/refresh cannot invoke or impersonate an approver/executor.

API references:
- https://yandex.ru/dev/metrika/en/stat/sampling
- https://yandex.ru/dev/metrika/en/stat/param
