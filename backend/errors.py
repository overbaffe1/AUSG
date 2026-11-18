"""Единый формат ошибок: {"error": {"code", "message", "details"}}.

Правило: любое исключение модели превращается в понятное оператору сообщение,
текущее состояние расчёта при этом не повреждается (критерий Т3).
"""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Ошибка, которую можно показать пользователю."""

    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, details: dict[str, Any] | None = None,
                 code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details}}


class NotFound(ServiceError):
    status_code = 404
    code = "NOT_FOUND"


class ValidationFailed(ServiceError):
    status_code = 422
    code = "VALIDATION_FAILED"


class EventRejected(ServiceError):
    """Событие отклонено целиком. Частичного применения не бывает."""

    status_code = 409
    code = "EVENT_REJECTED"


class RunFinished(ServiceError):
    status_code = 409
    code = "RUN_FINISHED"


def explain_model_error(exc: Exception) -> str:
    """Перевод технических сообщений модели в человекочитаемые."""
    text = str(exc)
    table = {
        "Unsupported scenario schema":
            "Неподдерживаемая схема сценария: ожидается cosmo-B-ops-1.0",
        "Event must arrive at the current unfinished step":
            "Событие относится к другому шагу: принимается только текущий невыполненный шаг",
        "Event id must be a new nonempty string":
            "Идентификатор события пустой или уже был получен ранее",
        "Time grid: 1..288 steps, 300 seconds per step":
            "Неверная сетка времени: допустимо 1..288 шагов по 300 секунд",
        "satellites must contain 1..48 objects":
            "Состав группировки: допустимо от 1 до 48 аппаратов",
        "A downlink job has exactly one eligible satellite":
            "У задания downlink должен быть ровно один допустимый исполнитель",
    }
    return table.get(text, text)
