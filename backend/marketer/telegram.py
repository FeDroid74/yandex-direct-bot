import json
import urllib.request


LABELS = {"pending": "Ожидает решения", "deferred": "Отложено", "applied": "Применено и проверено",
          "rejected": "Отклонено", "stale": "Данные изменились: нужна новая карточка", "expired": "Срок подтверждения истёк",
          "applying": "Применяется", "uncertain": "Нужна сверка результата, повтор запрещён",
          "accepted_manual": "План одобрен, требуется ручное выполнение", "completed_manual": "Выполнение отмечено пользователем"}


def action_text(body):
    a = body["action"]
    if a["kind"] == "add_negative":
        return f"Добавить минус-фразу: {a['phrase']}\nТолько в группу {a['ad_group_id']}"
    if a["kind"] == "ad_text":
        return f"Объявление {a['ad_id']}:\n" + "\n".join(f"{k}: {body['before'].get(k) or '(пусто)'} -> {v}" for k, v in a["fields"].items())
    if a["kind"] == "strategy_value":
        return f"{a['side']} / {a['field']}: {body['before']['value_micros']/1e6:g} -> {a['value_micros']/1e6:g} руб."
    return "Ручная работа: " + a["task"]


def card(row, details=False):
    b = row["body"]
    e, stats = b["evidence"], b["evidence"]["current"]
    conversions = f"{stats['Conversions']:g}" if stats.get("ConversionsComplete", True) else f"не менее {stats['Conversions']:g} (часть значений недоступна)"
    parts = [f"#{row['id']} · v{row['revision']} · {LABELS.get(row['state'], row['state'])}",
             b["title"], f"{b['campaign_name'][:120]} ({b['campaign_id']})", action_text(b),
             "Почему: " + b["reason"][:400],
             f"Факты {e['date_from']} — {e['date_to']}: {stats['Clicks']:g} кликов; {stats['Cost']:g} руб.; {conversions} достижений цели {e['goal_id']}. Атрибуция: {e['attribution']}.",
             "Гипотеза: " + b["expected_effect"][:180],
             f"Оценка через {b['evaluate_after_days']} дней: " + b["success_metric"][:230],
             "\n".join(b["warnings"])]
    if (row.get("result") or {}).get("error"):
        parts.append("Результат: " + row["result"]["error"][:600])
    if details:
        parts += ["Ограничения: " + " ".join(e["limits"]), "Исходные параметры: " + json.dumps(b["before"], ensure_ascii=False)]
    return "\n\n".join(parts)[:4000]


def buttons(row):
    suffix = f"{row['id']} {row['revision']}"
    def button(label, action):
        return {"text": label, "callback_data": f"/yd {action} {suffix}"}
    if row["state"] in ("pending", "deferred"):
        return [[button("Одобрить", "approve"), button("Отклонить", "reject")],
                [button("Изменить", "edit"), button("Отложить", "defer"), button("Подробнее", "details")]]
    if row["state"] == "accepted_manual":
        return [[button("Выполнено вручную", "done"), button("Подробнее", "details")]]
    return [[button("Подробнее", "details")]]


class Telegram:
    def __init__(self, token, owner_id):
        self.token, self.owner_id = token, str(owner_id)

    def call(self, method, payload):
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                         data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            if not data.get("ok"):
                raise RuntimeError("Telegram rejected request")
            return data["result"]
        except Exception as exc:
            # urllib exceptions include URLs containing bot tokens.
            raise RuntimeError(f"Telegram {method} failed ({type(exc).__name__})") from None

    def notify(self, message):
        return self.call("sendMessage", {"chat_id": self.owner_id, "text": message[:4000], "disable_web_page_preview": True})

    def publish(self, row, store):
        if row["delivery"] != "pending":
            return
        store.delivery(row["id"], "sending")
        try:
            result = self.call("sendMessage", {"chat_id": self.owner_id, "text": card(row),
                                              "reply_markup": {"inline_keyboard": buttons(row)}, "disable_web_page_preview": True})
            store.delivery(row["id"], "sent", result["message_id"])
        except Exception:
            store.delivery(row["id"], "uncertain")
            raise

    def refresh(self, row):
        if row.get("message_id"):
            self.call("editMessageText", {"chat_id": self.owner_id, "message_id": row["message_id"],
                                          "text": card(row), "reply_markup": {"inline_keyboard": buttons(row)}})
