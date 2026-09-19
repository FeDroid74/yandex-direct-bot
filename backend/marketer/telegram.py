import json
import re
import urllib.request


LABELS = {"pending": "Ожидает решения", "deferred": "Отложено", "applied": "Применено и проверено",
          "rejected": "Отклонено", "stale": "Данные изменились: нужна новая карточка", "expired": "Срок подтверждения истёк",
          "applying": "Применяется", "uncertain": "Нужна сверка результата, повтор запрещён",
          "accepted_manual": "План одобрен, требуется ручное выполнение", "completed_manual": "Выполнение отмечено пользователем"}


def telegram_length(value):
    return len(value.encode("utf-16-le")) // 2


def split_text(value, maximum):
    """Preserve every character, preferring whitespace boundaries for Telegram pages."""
    pages = []
    while telegram_length(value) > maximum:
        end, size = 0, 0
        for char in value:
            size += telegram_length(char)
            if size > maximum:
                break
            end += 1
        boundary = max(value.rfind("\n", 0, end), value.rfind(" ", 0, end))
        if boundary >= end // 2:
            end = boundary + 1
        pages.append(value[:end])
        value = value[end:]
    if value:
        pages.append(value)
    return pages


def summary(value, maximum):
    value = value.strip()
    if telegram_length(value) <= maximum:
        return value
    prefix = split_text(value, maximum - 1)[0].rstrip()
    # Summaries may omit a whole long word; the full text remains in details.
    if len(prefix) < len(value) and not value[len(prefix)].isspace():
        prefix = re.sub(r"\S+$", "", prefix).rstrip()
    return prefix + "…"


def header(row):
    return f"#{row['id']} · v{row['revision']} · {LABELS.get(row['state'], row['state'])}"


def action_text(body):
    a = body["action"]
    if a["kind"] == "add_negative":
        return f"Добавить минус-фразу: {a['phrase']}\nТолько в группу {a['ad_group_id']}"
    if a["kind"] == "ad_text":
        return f"Объявление {a['ad_id']}:\n" + "\n".join(f"{k}: {body['before'].get(k) or '(пусто)'} -> {v}" for k, v in a["fields"].items())
    if a["kind"] == "strategy_value":
        return f"{a['side']} / {a['field']}: {body['before']['value_micros']/1e6:g} -> {a['value_micros']/1e6:g} руб."
    return "Ручная работа: " + a["task"]


def facts(body):
    e, stats = body["evidence"], body["evidence"]["current"]
    goal = f"Цель {e['goal_id']} (Директ): "
    if stats.get("ConversionsComplete", True):
        goal += f"{stats['Conversions']:g} достижений."
    elif stats["Conversions"] > 0:
        goal += f"подтверждено не менее {stats['Conversions']:g} достижений; полная сумма недоступна."
    else:
        goal += "итог неизвестен, часть значений недоступна. Считать это нулём нельзя."
    return (f"Факты {e['date_from']} — {e['date_to']}: {stats['Clicks']:g} кликов; {stats['Cost']:g} руб.\n"
            f"{goal} Атрибуция Директа: {e['attribution']}.")


def explanation(body):
    effect = re.sub(r"^(?:гипотеза\s*:\s*)+", "", body["expected_effect"].strip(), flags=re.IGNORECASE)
    criterion = "Критерий выполнения" if body["action"]["kind"] == "advisory" else f"Оценка через {body['evaluate_after_days']} дней"
    return [("Почему", body["reason"], 400), ("Гипотеза", effect, 240), (criterion, body["success_metric"], 420)]


def card(row):
    b = row["body"]
    required = [header(row), b["title"], f"{b['campaign_name']} ({b['campaign_id']})", action_text(b), facts(b), "\n".join(b["warnings"])]
    optional = [f"{label}: {summary(value, maximum)}" for label, value, maximum in explanation(b)]
    if (row.get("result") or {}).get("error"):
        optional.append("Результат: " + summary(row["result"]["error"], 400))
    footer = "Полное обоснование и ограничения — в «Подробнее»."
    while True:
        result = "\n\n".join(required[:5] + optional + required[5:] + [footer])
        if telegram_length(result) <= 4000:
            return result
        if not optional:
            raise ValueError("Точное действие и предупреждения не помещаются в карточку. Сократите предложение.")
        optional.pop()


def detail_pages(row):
    b = row["body"]
    parts = [b["title"], f"{b['campaign_name']} ({b['campaign_id']})", action_text(b), facts(b)]
    parts += [f"{label}: {value}" for label, value, _ in explanation(b)]
    parts.append("\n".join(b["warnings"]))
    if b["evidence"].get("limits"):
        parts.append("Ограничения:\n" + "\n".join(b["evidence"]["limits"]))
    if b.get("before"):
        parts.append("Исходные параметры:\n" + json.dumps(b["before"], ensure_ascii=False, indent=2))
    if (row.get("result") or {}).get("error"):
        parts.append("Результат: " + row["result"]["error"])
    pages = split_text("\n\n".join(parts), 4000 - telegram_length(header(row)) - 80)
    return [f"{header(row)}\nПодробности · {index}/{len(pages)}\n\n{page}" for index, page in enumerate(pages, 1)]


def detail_response(row, page=1):
    pages = detail_pages(row)
    if type(page) is not int or not 1 <= page <= len(pages):
        raise ValueError("Такой страницы подробностей нет.")
    navigation = []
    for label, target in (("Назад", page - 1), ("Далее", page + 1)):
        if 1 <= target <= len(pages):
            navigation.append({"text": label, "callback_data": f"/yd details {row['id']} {row['revision']} {target}"})
    return {"text": pages[page - 1], "buttons": [navigation] if navigation else [], "page": page, "pages": len(pages)}


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
            return self.call("editMessageText", {"chat_id": self.owner_id, "message_id": row["message_id"],
                                                 "text": card(row), "reply_markup": {"inline_keyboard": buttons(row)}})
