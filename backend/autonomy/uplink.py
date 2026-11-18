"""Окна загрузки полётных заданий и раздача внутри группировки.

НАШЕ РАСШИРЕНИЕ МОДЕЛИ. Модель связности задаётся здесь явно, а не
берётся из полей сценария: relay_available означает возможность
выполнять оплачиваемую ретрансляцию, а не канал управления.

Правило по умолчанию (`gateway_broadcast`):
  - аппарат с downlink-контактом на шаге k — шлюз, он получает с Земли
    пакет бандлов;
  - на том же шаге пакет считается розданным всей группировке.

Это самая простая честная модель. Её можно ужесточить (раздача только
подмножеству, задержка на шаг) — и тогда цена автономии вырастет,
что само по себе интересный эксперимент.

### Кэш бандлов на Земле

Между загрузками полётное задание меняется мало: большая часть очереди
переезжает из бандла в бандл без изменений. Земля помнит, что именно
лежит на каждом борту, и передаёт только разницу — добавленные и
изменённые элементы плюс список снятых.

**Чего это НЕ даёт.** Энергии аппарата это не экономит: модель кейса
командный канал не моделирует вообще (`env.step` принимает команды для
всех аппаратов независимо от связи), а приём в нашем расширении не
тарифицируется. Энергию на борту тратит `downlink`/`relay` — оплачиваемая
работа, а не приём полётного задания.

**Что это даёт.** Объём командного трафика — величина нашего расширения,
и без кэша она неприлично большая: 659 КиБ на загрузку и 46 МиБ за смену
на P04_demand. Сокращение объёма делает модель связности правдоподобной и
показывает, что мы понимаем разницу между «передать меньше» и «потратить
меньше энергии».

Решения борта кэш не меняет ни на шаг: борт получает ровно тот же бандл,
просто собранный из базы и дельты. Закреплено тестом.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from backend.core.index import ScenarioIndex


@dataclass
class UplinkModel:
    """Когда и кому можно загрузить полётное задание."""

    policy: str = "gateway_broadcast"
    horizon_steps: int = 48

    def upload_steps(self, index: ScenarioIndex) -> list[int]:
        """Шаги, на которых вообще возможна загрузка с Земли."""
        return [k for k in range(index.steps) if index.contacts(k)]

    def can_upload(self, index: ScenarioIndex, sid: str, step: int) -> bool:
        contacts = index.contacts(step)
        if not contacts:
            return False
        if self.policy == "direct_only":
            return sid in contacts
        return True  # gateway_broadcast: шлюз раздаёт остальным


def _payload_bytes(obj) -> int:
    """Размер сообщения в байтах. Единая мерка для полного и дельты."""
    return len(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode())


@dataclass
class BundleCache:
    """Что Земля считает известным каждому борту.

    Кэш живёт на Земле: борт ничего дополнительно не хранит и не считает,
    он получает собранный бандл. Если Земля ошиблась и борт на деле знает
    другое, расхождение лечится полной загрузкой (``full_resync``).
    """

    # satellite_id -> {job_id: словарь элемента очереди}
    known: dict[str, dict[str, dict]] = field(default_factory=dict)
    # satellite_id -> прочие поля бандла (шлюзовые шаги, срок годности)
    known_header: dict[str, dict] = field(default_factory=dict)

    def diff(self, sid: str, bundle_dict: dict) -> dict:
        """Разница между новым бандлом и тем, что борт уже знает."""
        previous = self.known.get(sid, {})
        current = {item["job_id"]: item for item in bundle_dict["queue"]}

        added = [item for job_id, item in current.items()
                 if job_id not in previous]
        changed = [item for job_id, item in current.items()
                   if job_id in previous and previous[job_id] != item]
        removed = sorted(set(previous) - set(current))

        header = {k: v for k, v in bundle_dict.items() if k != "queue"}
        header_changed = header != self.known_header.get(sid)

        return {
            "satellite_id": sid,
            "header": header if header_changed else None,
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged_count": len(current) - len(added) - len(changed),
        }

    def commit(self, sid: str, bundle_dict: dict) -> None:
        """Запомнить, что борт теперь знает именно это."""
        self.known[sid] = {item["job_id"]: item for item in bundle_dict["queue"]}
        self.known_header[sid] = {k: v for k, v in bundle_dict.items()
                                  if k != "queue"}

    def forget(self, sid: str) -> None:
        """Сбросить кэш борта: следующая загрузка уйдёт полной."""
        self.known.pop(sid, None)
        self.known_header.pop(sid, None)


@dataclass
class UplinkLog:
    """Журнал загрузок — что именно аппарат знал и с какого момента.

    Считает и объём: сколько было бы передано без кэша и сколько
    передано на самом деле.
    """

    entries: list[dict] = field(default_factory=list)

    def record(self, step: int, gateway: str | None, satellites: list[str],
               horizon: int, bytes_full: int = 0, bytes_sent: int = 0,
               items_total: int = 0, items_sent: int = 0,
               items_removed: int = 0) -> None:
        self.entries.append({"step": step, "gateway": gateway,
                             "satellites": satellites, "horizon_steps": horizon,
                             "bytes_full": bytes_full, "bytes_sent": bytes_sent,
                             "items_total": items_total, "items_sent": items_sent,
                             "items_removed": items_removed})

    def last_upload_for(self, sid: str, step: int) -> int | None:
        last = None
        for entry in self.entries:
            if entry["step"] <= step and sid in entry["satellites"]:
                last = entry["step"]
        return last

    def volume(self) -> dict:
        """Сводка по объёму командного трафика.

        Это метрика НАШЕГО расширения, а не модели: энергии аппарата
        командный канал в модели не стоит (см. docstring модуля).
        """
        full = sum(e["bytes_full"] for e in self.entries)
        sent = sum(e["bytes_sent"] for e in self.entries)
        items_total = sum(e["items_total"] for e in self.entries)
        items_sent = sum(e["items_sent"] for e in self.entries)
        # Снятия считаются отдельно: снятый элемент — это один job_id,
        # а не полный элемент очереди, и складывать их с переданными
        # значило бы завышать объём.
        items_removed = sum(e.get("items_removed", 0) for e in self.entries)
        return {
            "uploads": len(self.entries),
            "bytes_without_cache": full,
            "bytes_sent": sent,
            "bytes_saved": full - sent,
            "saved_share": round(1 - sent / full, 4) if full else None,
            "items_without_cache": items_total,
            "items_sent": items_sent,
            "items_removed": items_removed,
        }

    def to_dict(self) -> dict:
        return {"uploads": self.entries, "volume": self.volume()}
