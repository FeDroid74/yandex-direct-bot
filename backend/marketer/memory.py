"""Decision memory derived from the audit log, never from invented chat summaries."""
import time


def brief(row, store):
    body = row["body"]
    observation = store.observation(row["id"])
    evaluation = observation["data"] if observation else next(
        (e["data"] for e in store.proposal_events(row["id"]) if e["event"] == "evaluation"), None)
    outcome = None
    if evaluation:
        outcome = {k: evaluation.get(k) for k in ("status", "conclusion", "before_from", "before_to", "date_from", "date_to", "days", "goal_id", "attribution", "confounders")}
        for period in ("before", "after"):
            if isinstance(evaluation.get(period), dict):
                outcome[period] = {k: evaluation[period].get(k) for k in ("Clicks", "Cost", "Conversions", "ConversionsComplete", "CPA")}
    return {"id": row["id"], "revision": row["revision"], "campaign_id": body["campaign_id"],
            "state": row["state"], "approval_expired": row["expires"] < time.time(),
            "title": body["title"], "action": body["action"], "created": row["created"],
            "updated": row["updated"], "applied_at": row["applied_at"],
            "reason": body.get("reason", "")[:400], "hypothesis": body.get("expected_effect", "")[:400],
            "success_metric": body.get("success_metric", "")[:400],
            "execution": {k: row["result"][k] for k in ("already_present", "error") if k in (row["result"] or {})},
            "evaluation": outcome,
            "manual_completion_is_not_proof_of_effect": body["action"]["kind"] == "advisory"}


def read_memory(store, campaign_id=0, page=1):
    if type(campaign_id) is not int or campaign_id < 0 or type(page) is not int or not 1 <= page <= 10000:
        raise ValueError("Invalid memory campaign/page")
    with store.db() as db:
        where, args = ("WHERE json_extract(body,'$.campaign_id')=?", [campaign_id]) if campaign_id else ("", [])
        total = db.execute("SELECT count(*) FROM proposals " + where, args).fetchone()[0]
        rows = [store.unpack(r) for r in db.execute(
            "SELECT * FROM proposals " + where + " ORDER BY created DESC,id DESC LIMIT 10 OFFSET ?", args + [(page-1)*10])]
    return {"items": [brief(r, store) for r in rows], "page": page, "total": total,
            "next_page": page+1 if page*10 < total else None,
            "limits": "Decisions are owner actions; application is API-verified; manual completion is owner-reported. Observations are not causal effects or paid sales."}


def memory_context(store, campaign_ids):
    items, total, counts = [], 0, {}
    for cid in campaign_ids:
        # Keep the model context bounded; the read tool exposes the complete paged log.
        with store.db() as db:
            counts[cid] = db.execute("SELECT count(*) FROM proposals WHERE json_extract(body,'$.campaign_id')=?", (cid,)).fetchone()[0]
            total += counts[cid]
            active = db.execute("SELECT * FROM proposals WHERE json_extract(body,'$.campaign_id')=? "
                                "AND state IN ('pending','deferred','accepted_manual','uncertain') ORDER BY updated DESC LIMIT 1", (cid,)).fetchall()
            evaluated = db.execute("SELECT p.* FROM proposals p LEFT JOIN experiment_observations o ON o.proposal_id=p.id "
                                   "WHERE json_extract(p.body,'$.campaign_id')=? AND (p.evaluated_at IS NOT NULL OR o.proposal_id IS NOT NULL) "
                                   "ORDER BY COALESCE(o.updated,p.evaluated_at) DESC LIMIT 1", (cid,)).fetchall()
            recent = db.execute("SELECT * FROM proposals WHERE json_extract(body,'$.campaign_id')=? ORDER BY updated DESC LIMIT 3", (cid,)).fetchall()
            selected = list({r["id"]: store.unpack(r) for r in active + evaluated + recent}.values())[:3]
        items.extend(brief(r, store) for r in selected)
    with store.db() as db:
        shared = [store.unpack(r) for r in db.execute(
            "SELECT * FROM proposals WHERE json_extract(body,'$.action.kind')='advisory' "
            "AND json_extract(body,'$.area')='measurement' AND state NOT IN ('failed','stale','expired') ORDER BY updated DESC LIMIT 8")]
    return {"items": items, "total": total, "counts": counts, "sampled": len(items) < total,
            "shared_measurement_tasks": [{"id": r["id"], "campaign_id": r["body"]["campaign_id"], "state": r["state"],
                                          "task": r["body"]["action"]["task"][:600]} for r in shared],
            "full_history_tool": "marketer_memory", "paid_orders_available": False}


def read_experiment(store, pid):
    row = store.get(pid)
    events = store.proposal_events(pid)
    return {"decision": brief(row, store), "observation": store.observation(pid),
            "events": [{"event": e["event"], "revision": e["revision"], "at": e["at"],
                        "actor": e["actor"], "data": e["data"] if e["event"] in ("experiment_observation", "evaluation") else
                        ({k: e["data"].get(k) for k in ("title", "action", "expected_effect", "success_metric")}
                         if e["event"] in ("created", "revised_requires_new_approval") else
                         {k: e["data"][k] for k in ("state", "error", "already_present") if k in e["data"]})}
                       for e in events], "events_limit": 30}
