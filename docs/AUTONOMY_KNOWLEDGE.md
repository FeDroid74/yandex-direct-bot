# Bounded autonomy and private course knowledge

Owner authorization: 2026-11-05, current task. This is prospective authorization
for the dedicated executor, not simulated Telegram approval or permission for
the analyst to execute shell writes.

## Advertising changes

From 2026-11-12, only a scheduled optimization slot may submit a new
`add_negative` or existing TextAd `ad_text` proposal to the executor.
Previously sent, revised, deferred, accepted or rejected cards are not converted
to automatic approvals. Interactive `/propose` remains an approval-card route.

Hard billing check before a write: counter 99041859, goal 352606262,
PAY_FOR_CONVERSION on every enabled strategy side. NETWORK_DEFAULT may inherit
only that purchase strategy. Unknown, click-billed, cart/lead, portfolio and
unsupported strategies fail closed. No automatic migration. CPA, budgets,
placement, launch, pause, product feed/filter changes remain owner-controlled.

Eligibility: fresh snapshot (24 hours), already running campaign, linked purchase
goal, no Metrika collection errors. Negatives need the full observed query,
at least two words, 30 mature clicks and numeric zero conversions including
the purchase goal. An ad edit needs an ON/ACCEPTED TextAd with 30 clicks.
These thresholds are operational safeguards, not proof of statistical significance.

An independent, isolated read-only model session must agree on meaning,
relevance and factual support. Unknown/invalid/timed-out review means an ordinary
owner card, not an automatic write. Course content never authorizes a write.

The executor rechecks live configuration, claims the exact proposal atomically,
records actor `automation:<policy_id>`, writes one action, re-reads the result
and checks billing/configuration again. Lost acknowledgements or post-write
uncertainty pause automation, never replay. Maximum three claims per rolling
seven days, and a 28-day campaign cooldown after applied changes.

`/yd auto pause|resume|status` is a native owner-only Telegram command. Pausing
does not undo an already dispatched operation. At 12:00 Europe/Podgorica a
calendar check claims at most one slot: every third local date from 2026-11-12
for at most one small change, and Mondays for account-wide analytics. On overlap
one snapshot serves both purposes. Off-days do not collect statistics or call a
model. The two review modes have separate data baselines. A light pass with no
eligible evidence stays quiet; a weekly report includes the verified change count
even when no new evidence warrants a model. A manual analysis is not an auto slot.
Completed analyses send a short summary and separate action details. Notification
uncertainty is recorded and not automatically resent. No automatic rollback.

API references used for billing and updates:
- https://yandex.ru/dev/direct/doc/ru/campaigns/get-unified-campaign
- https://yandex.com/dev/direct/doc/en/ads/update
- https://www.yandex.ru/support/direct/ru/troubleshooting/moderation

## Course knowledge

Private SQLite source/chunk/FTS tables live in `runtime/marketer.sqlite3`, not Git.
Sources keep course, lesson, URL, source date when known, locator and content hash.
Import is idempotent; inactive sources are excluded. Search is local and bounded,
without paid embeddings. Analysis receives at most four retrieved chunks. Model
proposals may cite up to four `knowledge_refs` from that snapshot; backend validates
them and preserves provenance in the details card.

`knowledge_import.py` accepts real TXT/MD/SRT/VTT transcripts. It does not treat a
video filename as watched content. `course_sync.py discover` follows accessible
lesson links only from the authorized course tree. It records locked lessons;
it never submits assignments, marks completion, or crawls account/profile data.
Session cookies are private (0600), not in source control. Bootstrap links and
passwords are never written into the repository.

`course_worker.py` processes accessible native GetCourse players sequentially.
It refuses protected/encrypted media, validates HTTPS CDN hosts and public IPs,
downloads only temporary audio, transcribes using local faster-whisper base/int8,
imports source-labelled excerpts and discards temporary audio. No paid ASR API.
The course timer starts this worker on Sundays at 04:00 Europe/Podgorica,
after rediscovering currently accessible lessons. It uses player signatures and
private transcript checkpoints, not a fresh transcription of every video.

## Telegram recovery (OpenClaw 2026.3.13)

Back up the direct session index, transcript, configuration and marketer DB
before maintenance. Keep the original transcript; detach only the diagnosed
overgrown owner DM while the gateway is stopped. Configure Telegram DM history
limit 16 and daily context renewal at 04:00 gateway-local. Course storage and
experiment history are separate. `deploy/openclaw_reply_guard.py ROOT BACKUP`
syntax-checks and backs up supported runtime bundles before replacement. On
compaction timeout the reply is an explicit error, never an old assistant text.
The gateway allows 15 seconds for a non-forwarded loopback handshake; auth,
network binding and permissions are unchanged. Recheck before upgrading OpenClaw.
CPUQuota=50%, MemoryMax=900M, one CPU thread, low priority. Audio is transcribed
in 3-minute memory-bounded chunks with source timestamps. It can take a long time.
System ffmpeg is required. Python dependencies are pinned separately; PyAV 16.1.0
is pinned for faster-whisper 1.2.1 compatibility. Speech recognition uses no paid API.
Failures remain `needs_attention`; expired auth stops work. Restart resumes
interrupted work from video transcript checkpoints, without importing duplicates.

Automatic speech transcripts can contain errors and do not capture all visual
demonstrations. Outdated course methods never override live API facts, purchase
billing or owner constraints. Full copyrighted course content stays private.
No model fine-tuning and no claim that unavailable lessons have been learned.

Operator commands on VPS (no secrets on command line):
```
python backend/course_sync.py status
systemctl --user status yandex-direct-course-import.service
systemctl --user stop yandex-direct-course-import.service
```
Use the dedicated course Python runtime for discovery/worker dependencies.
