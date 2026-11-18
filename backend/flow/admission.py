"""Допуск заданий в работу — регулирование потока.

Прямое следствие правила модели: частичная работа приносит ноль, но
ресурсы уже потрачены. Значит не начинать то, что не успеть, и не
начинать то, что убыточно.

Ключевое разделение (исправление З1, см. experiments/results/findings.md):
**допуск и ранжирование — разные вопросы.**

  - допуск отвечает «стоит ли это задание потраченных ресурсов вообще»,
    и сравнивает ПОЛНУЮ стоимость задания с теневой ценой ресурсов;
  - ранжирование отвечает «что делать раньше», и здесь стоимость
    взвешивается срочностью.

Раньше срочность умножала выручку, но не стоимость ресурсов. При
tightness < 1 (то есть у почти любого задания с запасом времени) выручка
уценивалась, а цена шага аппарата — нет, и допуск отклонял прибыльную
работу. В режиме ``priority_service`` это скрывалось множителем 1000 для
приоритета 3, в ``commercial`` — вылезало наружу: 8584 рабочих шага
против 10428 и минус $9 187 выручки на P04_demand.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.core.index import ScenarioIndex
from backend.flow import valuation
from backend.flow.cost import build_cost_card
from backend.flow.energy import EnergyForecast
from backend.flow.shadow_prices import ShadowPrices


@dataclass(frozen=True)
class Verdict:
    """Решение о допуске пары «аппарат — задание» на текущем шаге."""

    satellite_id: str
    job_id: str
    admitted: bool
    net_value: float      # ранжирование: с учётом срочности
    margin: float         # допуск: полная стоимость минус ресурсы
    reason: str
    detail: dict
    # Разряд режима (З8.2). Сравнение лексикографическое: сначала разряд,
    # при равенстве — деньги. Масштаб цен на выбор не влияет.
    tier: int = 0

    @property
    def sort_key(self) -> tuple[int, float, str, str]:
        """Порядок предпочтения: разряд, затем чистая ценность."""
        return (-self.tier, -self.net_value, self.job_id, self.satellite_id)

    def beats(self, other: "Verdict", by: float = 0.0) -> bool:
        """Строго лучше ``other`` с запасом ``by`` в долларах."""
        if self.tier != other.tier:
            return self.tier > other.tier
        return self.net_value - other.net_value > by

    def to_dict(self) -> dict:
        return {"satellite_id": self.satellite_id, "job_id": self.job_id,
                "admitted": self.admitted, "net_value": round(self.net_value, 4),
                "margin": round(self.margin, 4), "tier": self.tier,
                "reason": self.reason, "detail": self.detail}


def evaluate(index: ScenarioIndex, session, sid: str, job: dict, step: int,
             prices: ShadowPrices, objective: str, *,
             gamma: float = 2.0, critical_weight: float = 1000.0,
             tightness_limit: float = 1.0,
             forecast: EnergyForecast | None = None) -> Verdict:
    """Чистая ценность запуска:

        margin = value * PI(priority)
                 - lambda_G * ground_slots
                 - lambda_E(s) * energy_wh
                 - lambda_W(s) * remaining_steps

        NV     = margin, где выручка взвешена срочностью U(tightness):
                 ранжирование, а не допуск.
    """
    assessment = valuation.assess(index, job, step, gamma=gamma)
    state = session.env.state[sid]
    card = build_cost_card(index, sid, job, energy_wh=state["energy_wh"],
                           remaining_steps=assessment.remaining_steps)
    tier = valuation.priority_tier(int(job["priority"]), objective)
    # Обязательство перед приоритетом 3 в режиме приоритетного обслуживания
    # сильнее экономики: его не отбрасывают ни по цене, ни по запасу.
    protected = tier > 0
    started = assessment.remaining_steps < int(job["work_steps"])

    base_detail = {
        "opportunities": assessment.opportunities,
        "remaining_steps": assessment.remaining_steps,
        "tightness": assessment.tightness,
    }

    # 1. Не начинать то, что не успеть: окон меньше, чем осталось работы.
    # Это арифметика окон, а не «алгоритм не нашёл» (критерий О3).
    if assessment.provably_infeasible:
        return Verdict(sid, job["id"], False, 0.0, 0.0,
                       assessment.reason or "infeasible", base_detail, tier)

    # 2. Не начинать то, у чего нет права на ошибку. Задание с tightness
    # выше порога обязано идти на каждом оставшемся окне без единого сбоя;
    # один отказ по теплу или заряду — и ресурсы потрачены впустую.
    # Уже начатое не бросаем: вложенные шаги пропадут гарантированно.
    if not started and not protected and assessment.tightness > tightness_limit:
        return Verdict(sid, job["id"], False, 0.0, 0.0, "no_completion_margin",
                       {**base_detail, "tightness_limit": tightness_limit}, tier)

    # 3. Не начинать то, на что не хватит заряда. Прогноз повторяет
    # арифметику модели по известному солнцу (flow/energy.py).
    #
    # Нехватка заряда на горизонте — приговор только тогда, когда
    # перезарядиться уже некогда: срок задания внутри горизонта прогноза.
    # Если срок дальше, аппарат успеет добрать энергии на солнце, и
    # отказывать заранее значит терять выручку — на P03_energy ровно этот
    # чрезмерно строгий вариант стоил $877 (см. findings.md).
    if forecast is not None and not started and not protected:
        available = forecast.steps_for(sid, job["kind"])
        deadline_inside = int(job["deadline_step"]) - step <= forecast.horizon
        if available <= 0 or (deadline_inside and not forecast.covers(
                sid, job["kind"], assessment.remaining_steps)):
            return Verdict(sid, job["id"], False, 0.0, 0.0, "energy_forecast_short",
                           {**base_detail, "affordable_steps": available,
                            "deadline_inside_horizon": deadline_inside}, tier)

    gross = assessment.value_usd
    cost = (prices.ground_slot * card.ground_slots
            + prices.energy_wh.get(sid, 0.0) * card.energy_wh
            + prices.sat_step.get(sid, 0.0) * card.sat_steps)
    margin = gross - cost
    # Срочность двигает задание в очереди, но не решает, брать ли его.
    #
    # Внутри защищённого разряда теневая цена НЕ вычитается. Цель режима
    # сформулирована организаторами так: «завершить в срок как можно
    # больше заданий приоритета 3. При сопоставимом выполнении этих
    # заданий учитывается выручка». То есть деньги здесь тайбрейк, а не
    # соразмерная величина, и ресурсоёмкость не может стоить обязательства.
    #
    # Разряд и так доминирует над обычными заданиями, но вычитание цены
    # переставляло P3-задания МЕЖДУ СОБОЙ по дешевизне ресурса: дорогие
    # по энергии уходили в конец очереди и четыре из них не успевали.
    #
    # Внутри разряда вычитается только цена ЭНЕРГИИ, и это не полумера.
    # lambda_W и lambda_G — альтернативная стоимость против других заданий,
    # в основном обычных; разряд и так решил этот спор в пользу P3, и
    # вычитать их повторно значит уценивать обязательство деньгами.
    # Энергия — другое: её запас конечен физически, и потраченная на
    # дорогое P3 она отнимает ДРУГОЕ P3. Это спор внутри разряда, а не
    # между разрядами, поэтому он учитывается.
    if protected:
        net = gross * assessment.urgency - prices.energy_wh.get(sid, 0.0) * card.energy_wh
    else:
        net = gross * assessment.urgency - cost

    detail = {
        **base_detail,
        "gross": round(gross, 4), "cost": round(cost, 4),
        "margin": round(margin, 4),
        "urgency": assessment.urgency,
        "priority_tier": tier,
        "started": started,
        "cost_card": card.to_dict(),
    }

    # 4. Не начинать то, что уступает другим вариантам использования
    # ресурсов по оценке планировщика. Это не «убыточно» в бухгалтерском
    # смысле: теневые цены — внутренняя оценка ценности, а не расходы
    # в долларах (правка внешнего аудита, З8.3).
    if margin <= 0 and not protected:
        return Verdict(sid, job["id"], False, net, margin, "negative_value",
                       detail, tier)

    return Verdict(sid, job["id"], True, net, margin, "accepted", detail, tier)


def preemption_gain(current: Verdict, challenger: Verdict,
                    work_done_steps: int, price_per_step: float,
                    hysteresis: float = 0.0) -> float:
    """Выигрыш от вытеснения начатого задания.

    Вытеснять имеет смысл, только если выигрыш перекрывает уже потраченную
    работу — иначе мы просто конвертируем ресурсы в ноль дважды.

    ``hysteresis`` — порог безразличия в долларах. Без него два почти
    равных задания вытесняют друг друга на соседних шагах, и оба остаются
    незавершёнными.

    Разряды сравниваются первыми (З8.2): задание приоритета 3 в режиме
    приоритетного обслуживания вытесняет обычное независимо от денег, и
    наоборот — обычное не вытесняет приоритетное ни при какой стоимости.
    """
    if challenger.tier != current.tier:
        return 1.0 if challenger.tier > current.tier else -1.0
    sunk = work_done_steps * price_per_step
    return challenger.net_value - current.net_value - sunk - hysteresis
