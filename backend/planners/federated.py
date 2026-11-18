"""Федеративный режим: ЦУП формирует полётные задания, борт исполняет сам.

НАШЕ РАСШИРЕНИЕ МОДЕЛИ. Модель кейса не моделирует командный канал —
env.step(actions) принимает команды для всех аппаратов независимо от
связи. Здесь это ограничение вводится явно:

  - бандл загружается ТОЛЬКО на шаге, где есть контакт с Землёй;
  - вне контакта аппарат выбирает действие сам, по последнему
    загруженному полётному заданию и собственному состоянию;
  - глобальный лимит наземного сегмента раскладывается по бортам
    заранее (borta не могут договориться на лету);
  - задания в бандлах не пересекаются, поэтому два аппарата не возьмут
    одно и то же задание на одном шаге.

Разница метрик с централизованным режимом — это цена автономии.

Демо-кейс: P01_intro, где группировка не видит Землю 85% смены,
максимальный разрыв 37 шагов подряд (более трёх часов).
"""
from __future__ import annotations

import copy
from typing import Any

from backend.autonomy import bundle as bundle_mod
from backend.autonomy.bundle import Bundle
from backend.autonomy.onboard_agent import decide_onboard
from backend.autonomy.uplink import (BundleCache, UplinkLog, UplinkModel,
                                     _payload_bytes)
from backend.core.index import ScenarioIndex
from backend.flow import admission, energy, shadow_prices, valuation
from backend.planners.base import Decision, PlannerConfig
from backend.planners.baseline import select_gateway


class FederatedPlanner:
    """Держит состояние между шагами: бандлы живут дольше одного вызова."""

    name = "federated"
    version = "0.2"

    def __init__(self, horizon_steps: int = 48, replan_gap: int = 12,
                 uplink: UplinkModel | None = None,
                 backup_executors: int = 3, backup_delay: int = 1,
                 cache_uplink: bool = True, skip_speculative: bool = True) -> None:
        self.horizon_steps = horizon_steps
        self.replan_gap = replan_gap
        # Сколько исполнителей получает задание: основной плюс запасные.
        # Главная потеря при редкой загрузке была в том, что задание лежало
        # ровно в одном бандле и просто ждало занятого аппарата (З5).
        self.backup_executors = max(1, backup_executors)
        self.backup_delay = max(0, backup_delay)
        self.uplink = uplink or UplinkModel(horizon_steps=horizon_steps)
        # Кэш бандлов на Земле: передаём только разницу с тем, что борт уже
        # знает. Решения борта от этого не меняются — он получает тот же
        # бандл, просто собранный из базы и дельты. Экономит объём
        # командного трафика, но НЕ энергию аппарата: приём полётного
        # задания в модели не тарифицируется (см. autonomy/uplink.py).
        self.cache_uplink = cache_uplink
        # Не начинать то, что по расчёту Земли не влезает в ёмкость борта.
        self.skip_speculative = skip_speculative
        self.cache = BundleCache()
        self.bundles: dict[str, Bundle] = {}
        self.log = UplinkLog()
        self._last_upload_step: int | None = None

    # ------------------------------------------------------------------ цикл

    def decide(self, session, index: ScenarioIndex,
               cfg: PlannerConfig) -> dict[str, Decision]:
        step = session.env.k
        index.refresh(step, session.env.jobs)

        if self._should_upload(session, index, step):
            self._upload(session, index, cfg, step)

        decisions: dict[str, Decision] = {}
        for sid in index.sat_ids:
            decision = decide_onboard(session, index, sid, self.bundles.get(sid),
                                      cfg.calibration_margin_steps,
                                      skip_speculative=self.skip_speculative)
            decision.explain["last_uplink_step"] = self.log.last_upload_for(sid, step)
            decisions[sid] = decision

        # Страховка: бандлы строились на прошлом контакте и могли устареть.
        # Если два борта всё же нацелились на одно задание, уступает тот,
        # у кого оно ниже в очереди.
        self._resolve_collisions(session, index, decisions)
        return decisions

    # ------------------------------------------------------------------ связь

    def _should_upload(self, session, index: ScenarioIndex, step: int) -> bool:
        if not self.uplink.can_upload(index, index.sat_ids[0], step):
            return False  # связи нет физически — загружать нечего и некому
        if not self.bundles:
            return True
        if self._last_upload_step is None:
            return True
        if step - self._last_upload_step >= self.replan_gap:
            return True
        return any(b.expired(step) for b in self.bundles.values())

    def _upload(self, session, index: ScenarioIndex, cfg: PlannerConfig,
                step: int) -> None:
        """Наземный планировщик пересобирает полётные задания.

        Считается по полной картине — это и есть работа ЦУПа. Дальше
        аппараты живут по результату до следующего окна связи.
        """
        env = session.env
        forecast = energy.build(index, session, step, cfg.lookahead_steps)
        prices = shadow_prices.estimate(index, session, step,
                                        cfg.soc_safety_margin_pct,
                                        cfg.lookahead_steps, forecast=forecast)
        horizon_stop = min(index.steps, step + self.horizon_steps)

        # 1. Для каждого задания выбрать одного исполнителя — бандлы
        #    не пересекаются, поэтому конфликтов на шаге не будет.
        # Задание кладётся в бандлы нескольких исполнителей с разными
        # рангами: основной идёт первым, запасной берётся за дело, если
        # основной за ним так и не пришёл. Коллизии снимает
        # _resolve_collisions, поэтому дублирование безопасно (З5).
        # В бандл идут не только задания, выпущенные сейчас, но и те, что
        # выйдут до конца горизонта: расписание выпуска известно из
        # сценария, и именно в этом смысл полётного задания — аппарат без
        # связи должен знать, что ему делать через десять шагов.
        # Раньше бандл собирался только по index.active, и всё выпущенное
        # после загрузки просто ждало следующего окна связи — это и была
        # главная потеря при редкой загрузке (З5).
        upcoming: set[str] = set(index.active)
        for k in range(step, horizon_stop):
            upcoming.update(index.jobs_by_release.get(k, ()))

        scored: list[tuple[tuple[int, float], str, list[str]]] = []
        for job_id in sorted(upcoming):
            job = env.jobs.get(job_id)
            if job is None or job["completed_step"] is not None:
                continue
            if job["release_step"] >= horizon_stop:
                continue
            if job["deadline_step"] <= step:
                continue
            # Ещё не выпущенное задание оценивается на шаг его выпуска:
            # на текущем шаге окон у него формально ноль.
            from_step = max(step, int(job["release_step"]))
            ranked_sats: list[tuple[tuple[int, float], str]] = []
            for sid in job["eligible_satellites"]:
                if sid not in index.sats:
                    continue
                if index.remaining_windows(sid, job["kind"], from_step,
                                           horizon_stop) <= 0:
                    continue
                verdict = admission.evaluate(
                    index, session, sid, job, from_step, prices, cfg.objective,
                    gamma=cfg.urgency_gamma, critical_weight=cfg.critical_weight,
                    tightness_limit=cfg.admission_tightness_limit,
                    forecast=forecast)
                if not verdict.admitted:
                    continue
                ranked_sats.append(((verdict.tier, verdict.net_value), sid))
            if not ranked_sats:
                continue
            ranked_sats.sort(key=lambda pair: (-pair[0][0], -pair[0][1], pair[1]))
            # downlink закреплён за единственным допустимым исполнителем,
            # запасных у него нет по устройству данных.
            limit = 1 if job["kind"] == "downlink" else self.backup_executors
            scored.append((ranked_sats[0][0], job_id,
                           [sid for _, sid in ranked_sats[:limit]]))

        # Лексикографически: разряд режима, затем чистая ценность.
        scored.sort(key=lambda t: (-t[0][0], -t[0][1], t[1]))

        # Ёмкость борта на горизонте: больше шагов, чем у него есть окон,
        # он всё равно не отработает. Без этого ограничения каждое задание
        # независимо выбирает лучшего исполнителя, вся работа сваливается
        # на один-два борта, а остальные простаивают — на P04_demand при
        # загрузке раз в шаг это стоило $14 975 (см. findings.md).
        capacity = {
            sid: max(0, min(horizon_stop - step,
                            max(index.remaining_windows(sid, "relay", step,
                                                        horizon_stop),
                                index.remaining_windows(sid, "downlink", step,
                                                        horizon_stop))))
            for sid in index.sat_ids
        }
        load: dict[str, int] = {sid: 0 for sid in index.sat_ids}

        assignments: dict[str, str] = {}   # job_id -> основной исполнитель
        per_sat: dict[str, list[str]] = {sid: [] for sid in index.sat_ids}
        backup_ranks: dict[str, dict[str, int]] = {sid: {} for sid in index.sat_ids}
        # Задания, попавшие в бандл СВЕРХ расчётной ёмкости борта. Борт их
        # не начинает: он до них дойдёт только случайно и почти наверняка
        # бросит, а частичная работа не оплачивается.
        speculative: dict[str, set[str]] = {sid: set() for sid in index.sat_ids}

        for _, job_id, sids in scored:
            need = int(env.jobs[job_id]["remaining_steps"])
            # Основным становится первый исполнитель, у которого ещё есть
            # ёмкость; если свободных нет, задание всё равно отдаётся
            # лучшему — пусть лучше стоит в очереди, чем нигде, но тогда
            # оно помечается спекулятивным.
            primary = next((sid for sid in sids
                            if load[sid] + need <= capacity[sid]), None)
            if primary is None:
                primary = sids[0]
                # Обязательство перед приоритетом 3 в режиме приоритетного
                # обслуживания сильнее расчёта ёмкости: такое задание борт
                # берёт даже сверх ёмкости. То же исключение, что в
                # admission.evaluate, — иначе режимы противоречат друг другу.
                if not valuation.priority_tier(int(env.jobs[job_id]["priority"]),
                                               cfg.objective):
                    speculative[primary].add(job_id)
            ordered = [primary] + [sid for sid in sids if sid != primary]
            assignments[job_id] = primary
            load[primary] += need
            for rank, sid in enumerate(ordered):
                per_sat[sid].append(job_id)
                backup_ranks[sid][job_id] = rank

        # 2. Разложить глобальный лимит наземного сегмента по бортам.
        reservations = bundle_mod.reserve_ground_slots(
            index, session, assignments, step, self.horizon_steps)

        # 3. Роли шлюза на весь горизонт — аппарат исполнит их без связи.
        gateway_by_step: dict[int, str] = {}
        for k in range(step, horizon_stop):
            contacts = index.contacts(k)
            if contacts:
                gateway_by_step[k] = min(
                    contacts,
                    key=lambda s: (index.remaining_windows(s, "downlink", k,
                                                           index.steps), s))

        # 4. Собрать бандлы.
        for sid in index.sat_ids:
            # Основные задания идут раньше запасных: своя работа важнее
            # подстраховки соседа.
            ranked = sorted(
                per_sat[sid],
                key=lambda j: (backup_ranks[sid].get(j, 0),
                               env.jobs[j]["deadline_step"],
                               -float(env.jobs[j]["value_usd"]), j))
            gateway_steps = {k for k, owner in gateway_by_step.items() if owner == sid}
            self.bundles[sid] = bundle_mod.build(
                index, session, sid, step, self.horizon_steps, ranked,
                gateway_steps=gateway_steps,
                reservations={j: reservations[j] for j in ranked if j in reservations},
                backup_ranks=backup_ranks[sid],
                backup_delay=self.backup_delay,
                forecast=forecast,
                speculative=speculative[sid],
            )

        gateway_now = select_gateway(session, index, step)

        # Объём командного трафика: что ушло бы целиком и что ушло на деле.
        bytes_full = bytes_sent = items_total = items_sent = 0
        items_removed = 0
        for sid in index.sat_ids:
            # Мерка — проводной вид: производные величины борт считает сам
            # и по каналу они не идут (см. BundleItem.wire_dict).
            payload = self.bundles[sid].wire_dict()
            bytes_full += _payload_bytes(payload)
            items_total += len(payload["queue"])
            if self.cache_uplink:
                delta = self.cache.diff(sid, payload)
                delta_bytes = _payload_bytes(delta)
                full_bytes = _payload_bytes(payload)
                # Дельта не всегда короче: при редкой загрузке очередь
                # успевает обновиться целиком, совпадений нет, а список
                # снятых идёт сверху. На replan_gap = 48 такая «экономия»
                # была отрицательной (−9.3%), поэтому берём меньшее из
                # двух — кэш не может ухудшить объём.
                if delta_bytes < full_bytes:
                    bytes_sent += delta_bytes
                    items_sent += len(delta["added"]) + len(delta["changed"])
                    items_removed += len(delta["removed"])
                else:
                    bytes_sent += full_bytes
                    items_sent += len(payload["queue"])
                self.cache.commit(sid, payload)
            else:
                bytes_sent += _payload_bytes(payload)
                items_sent += len(payload["queue"])

        self.log.record(step, gateway_now, list(index.sat_ids),
                        self.horizon_steps, bytes_full=bytes_full,
                        bytes_sent=bytes_sent, items_total=items_total,
                        items_sent=items_sent, items_removed=items_removed)
        self._last_upload_step = step

    # ------------------------------------------------------------------ защита

    def _resolve_collisions(self, session, index: ScenarioIndex,
                            decisions: dict[str, Decision]) -> None:
        """Снять конфликты, если бандлы устарели после событий.

        Планировщик обязан сам согласовывать назначения, а не полагаться
        на отбор конфликтов библиотекой (описание данных, с. 8).
        """
        from backend.planners.base import IDLE

        limit = int(index.model["downlink_parallel_limit"])
        taken: dict[str, str] = {}
        downlink_used = 0

        for sid in index.sat_ids:
            decision = decisions[sid]
            action = decision.action
            if action.get("action") != "job":
                continue
            job_id = action["job_id"]
            job = session.env.jobs.get(job_id)
            if job is None:
                continue

            if job_id in taken:
                decision.action = IDLE
                decision.reason_code = "preempted_by"
                decision.explain["conflict_with"] = taken[job_id]
                continue
            if job["kind"] == "downlink":
                if downlink_used >= limit:
                    decision.action = IDLE
                    decision.reason_code = "ground_capacity_reserved"
                    continue
                downlink_used += 1
            taken[job_id] = sid

    # ------------------------------------------------------------------ вывод

    def snapshot(self) -> dict[str, Any]:
        return {"bundles": [b.to_dict() for b in self.bundles.values()],
                "uplink": self.log.to_dict(),
                "cache_uplink": self.cache_uplink,
                "skip_speculative": self.skip_speculative,
                "horizon_steps": self.horizon_steps,
                "replan_gap": self.replan_gap,
                "backup_executors": self.backup_executors,
                "policy": self.uplink.policy}

    def __deepcopy__(self, memo) -> "FederatedPlanner":
        """Ветвь получает собственную копию бандлов — ветви независимы."""
        clone = FederatedPlanner(self.horizon_steps, self.replan_gap,
                                 copy.deepcopy(self.uplink, memo),
                                 self.backup_executors, self.backup_delay,
                                 self.cache_uplink, self.skip_speculative)
        # Ветвь получает собственный кэш: иначе Земля считала бы, что борт
        # соседней ветви уже знает то, чего ему не передавали.
        clone.cache = copy.deepcopy(self.cache, memo)
        clone.bundles = copy.deepcopy(self.bundles, memo)
        clone.log = copy.deepcopy(self.log, memo)
        clone._last_upload_step = self._last_upload_step
        return clone
