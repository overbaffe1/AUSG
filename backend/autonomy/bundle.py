"""Полётные задания (бандлы) — НАШЕ РАСШИРЕНИЕ МОДЕЛИ.

Граница ответственности, которую нужно называть своим именем на защите:

  - Модель кейса НЕ моделирует командный канал. env.step(actions)
    принимает команды для всех аппаратов на каждом шаге независимо
    от связи. Ограничения downlink_available / relay_available
    действуют на ВЫПОЛНЕНИЕ ЗАДАНИЙ, не на доставку команд.
  - relay в модели — это ТИП ОПЛАЧИВАЕМОГО ЗАДАНИЯ, а не межспутниковый
    канал управления. Описание данных, с. 5: «Для ретрансляции не
    требуется строить пары спутников или маршруты».

Поэтому раздача бандлов — отдельная сущность с собственной моделью
связности, а не переиспользование полей сценария. Итоговые действия
всё равно проходят через env.step со всеми проверками, поэтому учёт
и replay_episode остаются эталонными.

ЭТАП 2 (Dev A).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.index import ScenarioIndex
from backend.flow.cost import CostCard


@dataclass
class BundleItem:
    job_id: str
    priority_rank: int
    earliest_step: int
    latest_start_step: int      # позже начинать бессмысленно — не успеть
    min_soc_pct: float          # не начинать ниже этого заряда
    expected_cost: CostCard | None = None
    # Заранее выделенные шаги наземного сегмента. Бортовые агенты не могут
    # договориться между собой на лету, поэтому глобальный лимит в два
    # downlink на шаг раскладывается на борта заранее, на Земле.
    reserved_steps: frozenset[int] | None = None
    # Запасной исполнитель (З5): аппарат берёт задание, только если
    # основной за ним так и не пришёл. Ранг 0 — основной.
    backup_rank: int = 0
    # Задание освобождается для запасного не раньше этого шага: у основного
    # есть фора, иначе два борта возьмутся за одно и то же, и половина
    # работы уйдёт в отбраковку коллизий.
    backup_release_step: int = 0
    # Условие отказа (З5). Борт проверяет его сам, без связи с Землёй:
    # сколько окон нужного канала у НЕГО остаётся до срока задания.
    abandon_after_step: int = 0          # срок задания
    abandon_min_windows: int = 0         # нужно окон, чтобы успеть
    # Признак «не окупится», предвычисленный на Земле.
    #
    # Земля знает ёмкость борта на горизонте (число его окон) и порядок, в
    # котором он будет разбирать очередь. Если к моменту этого элемента
    # расчётная загрузка борта уже исчерпана, элемент лежит в бандле
    # СВЕРХ ёмкости: борт до него дойдёт только случайно, начнёт и почти
    # наверняка бросит. Начинать такое — прямой убыток, потому что
    # частичная работа не оплачивается, а энергия потрачена.
    #
    # Борт не НАЧИНАЕТ спекулятивный элемент, но НАЧАТОЕ добивает:
    # вложенные шаги пропадут гарантированно, если бросить.
    speculative: bool = False

    def allowed_at(self, step: int) -> bool:
        if not (self.earliest_step <= step <= self.latest_start_step):
            return False
        if self.backup_rank and step < self.backup_release_step:
            return False
        return self.reserved_steps is None or step in self.reserved_steps

    def abandon_reason(self, index, sid: str, kind: str, step: int,
                       remaining_steps: int) -> str | None:
        """Почему это задание уже не стоит вести. None — стоит.

        Считается арифметикой окон, а не «не получилось»: борт сравнивает
        остаток работы с числом собственных окон до срока.
        """
        if step >= self.abandon_after_step:
            return "deadline_passed"
        if step > self.latest_start_step:
            return "too_late_to_start"
        windows = index.remaining_windows(sid, kind, step, self.abandon_after_step)
        if windows < remaining_steps:
            return "not_enough_windows"
        return None

    def to_dict(self) -> dict:
        """Полный вид элемента — для интерфейса и выгрузки."""
        return {"job_id": self.job_id, "priority_rank": self.priority_rank,
                "earliest_step": self.earliest_step,
                "latest_start_step": self.latest_start_step,
                "min_soc_pct": self.min_soc_pct,
                "backup_rank": self.backup_rank,
                "backup_release_step": self.backup_release_step,
                "reserved_steps": (sorted(self.reserved_steps)
                                   if self.reserved_steps else None),
                "speculative": self.speculative,
                "abandon_if": {"after_step": self.abandon_after_step,
                               "min_windows": self.abandon_min_windows}}

    def wire_dict(self) -> dict:
        """То, что реально уходит на борт по командному каналу.

        Отличие от ``to_dict`` — выброшены ПРОИЗВОДНЫЕ величины, которые
        борт вычисляет сам:

          - ``backup_release_step`` = issued_at_step + backup_delay * rank,
            обе части есть в заголовке бандла;
          - ``priority_rank`` — это индекс в очереди, а порядок борт
            восстанавливает сортировкой по тем же ключам, по которым его
            задала Земля.

        Смысл ровно один: эти два поля меняются при КАЖДОЙ загрузке просто
        потому, что изменился её шаг. Пока они в сообщении, кэш на Земле
        бесполезен — не совпадает ни один элемент из 26 953. Без них
        совпадает 94%.
        """
        return {"job_id": self.job_id,
                "earliest_step": self.earliest_step,
                "latest_start_step": self.latest_start_step,
                "min_soc_pct": self.min_soc_pct,
                "backup_rank": self.backup_rank,
                "reserved_steps": (sorted(self.reserved_steps)
                                   if self.reserved_steps else None),
                "speculative": self.speculative,
                "abandon_if": {"after_step": self.abandon_after_step,
                               "min_windows": self.abandon_min_windows}}


@dataclass
class Bundle:
    satellite_id: str
    issued_at_step: int
    valid_until_step: int
    queue: list[BundleItem] = field(default_factory=list)
    gateway_steps: set[int] = field(default_factory=set)
    fallback: str = "recover_energy"
    # Задержка запасного исполнителя живёт в заголовке, а не в каждом
    # элементе: иначе она тянет за собой шаг загрузки и ломает кэш.
    backup_delay: int = 1

    def expired(self, step: int) -> bool:
        return step >= self.valid_until_step

    def wire_dict(self) -> dict:
        """Бандл в том виде, в каком он передаётся на борт."""
        return {
            "satellite_id": self.satellite_id,
            "issued_at_step": self.issued_at_step,
            "valid_until_step": self.valid_until_step,
            "backup_delay": self.backup_delay,
            "gateway_steps": sorted(self.gateway_steps),
            "fallback": self.fallback,
            "backup_delay": self.backup_delay,
            "queue": [item.wire_dict() for item in self.queue],
        }

    def to_dict(self) -> dict:
        return {
            "satellite_id": self.satellite_id,
            "issued_at_step": self.issued_at_step,
            "valid_until_step": self.valid_until_step,
            "gateway_steps": sorted(self.gateway_steps),
            "fallback": self.fallback,
            "backup_delay": self.backup_delay,
            "queue": [item.to_dict() for item in self.queue],
        }


def build(index: ScenarioIndex, session, sid: str, step: int, horizon: int,
          ranked_job_ids: list[str], *,
          gateway_steps: set[int] | None = None,
          reservations: dict[str, frozenset[int]] | None = None,
          backup_ranks: dict[str, int] | None = None,
          backup_delay: int = 1,
          forecast=None,
          speculative: set[str] | None = None) -> Bundle:
    """Собрать полётное задание для аппарата на горизонт вперёд.

    Роль шлюза закладывается заранее: наземный планировщик знает
    расписание окон контакта из входных данных, поэтому аппарат
    исполняет её и без связи с Землёй.
    """
    valid_until = min(index.steps, step + horizon)
    if gateway_steps is None:
        gateway_steps = set()
    reservations = reservations or {}

    backup_ranks = backup_ranks or {}
    speculative = speculative or set()
    reserve_pct = float(index.model["reserve_soc_pct"])

    queue: list[BundleItem] = []
    for rank, job_id in enumerate(ranked_job_ids):
        job = session.env.jobs.get(job_id)
        if job is None or job["completed_step"] is not None:
            continue
        # Позже этого шага начинать бессмысленно: не успеть до срока.
        latest = job["deadline_step"] - job["remaining_steps"]
        backup_rank = backup_ranks.get(job_id, 0)
        # Порог заряда берётся из прогноза, а не константой: на смене с
        # просадкой порог выше резерва ровно на предсказанную нехватку,
        # на солнечной — равен резерву (З5).
        min_soc = (forecast.min_soc_pct(sid, reserve_pct) if forecast is not None
                   else reserve_pct)
        queue.append(BundleItem(
            job_id=job_id,
            priority_rank=rank,
            earliest_step=max(step, job["release_step"]),
            latest_start_step=latest,
            min_soc_pct=min_soc,
            reserved_steps=reservations.get(job_id),
            backup_rank=backup_rank,
            backup_release_step=step + backup_delay * backup_rank,
            abandon_after_step=int(job["deadline_step"]),
            abandon_min_windows=int(job["remaining_steps"]),
            speculative=job_id in speculative,
        ))

    return Bundle(satellite_id=sid, issued_at_step=step,
                  valid_until_step=valid_until, queue=queue,
                  gateway_steps=set(gateway_steps), backup_delay=backup_delay)


def reserve_ground_slots(index: ScenarioIndex, session, assignments: dict[str, str],
                         step: int, horizon: int) -> dict[str, frozenset[int]]:
    """Разложить глобальный лимит наземного сегмента по бортам заранее.

    Бортовые агенты работают без связи и не могут договориться на лету,
    поэтому конфликт за два downlink-слота на шаг разрешается на Земле:
    каждому downlink-заданию выдаются конкретные шаги.

    ``assignments``: job_id -> satellite_id.
    """
    limit = int(index.model["downlink_parallel_limit"])
    stop = min(index.steps, step + horizon)
    used: dict[int, int] = {}
    reservations: dict[str, frozenset[int]] = {}

    downlink_jobs = [
        (job_id, sid) for job_id, sid in assignments.items()
        if (job := session.env.jobs.get(job_id)) is not None
        and job["kind"] == "downlink"
    ]
    # Сначала те, у кого срок ближе: им выбирать не из чего.
    downlink_jobs.sort(key=lambda pair: session.env.jobs[pair[0]]["deadline_step"])

    for job_id, sid in downlink_jobs:
        job = session.env.jobs[job_id]
        need = job["remaining_steps"]
        window = range(max(step, job["release_step"]),
                       min(stop, job["deadline_step"]))
        picked: list[int] = []
        for k in window:
            if len(picked) >= need:
                break
            if used.get(k, 0) >= limit:
                continue
            if not index.env[sid]["downlink_available"][k]:
                continue
            picked.append(k)
            used[k] = used.get(k, 0) + 1
        reservations[job_id] = frozenset(picked)
    return reservations
