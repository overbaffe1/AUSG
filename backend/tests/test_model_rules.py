"""Проверки правил модели. Здесь ловятся ошибки, дорогие по критерию Т1."""
from __future__ import annotations

import copy

import pytest

from model.operations import Session, replay_episode
from model.resource_env import Environment

from backend.core.index import ScenarioIndex
from backend.core.scenario_store import STORE
from backend.flow import preflight, valuation
from backend.flow.cost import build_cost_card, energy_per_step_wh
from backend.planners.base import IDLE, PlannerConfig
from backend.planners.baseline import BaselinePlanner, select_gateway
from backend.planners.economic import EconomicPlanner


@pytest.fixture(scope="module")
def intro() -> dict:
    return copy.deepcopy(STORE.get("P01_intro").scenario)


@pytest.fixture(scope="module")
def shift() -> dict:
    return copy.deepcopy(STORE.get("P02_shift").scenario)


# ------------------------------------------------------------------ параметры

def test_model_params_come_from_scenario_not_from_the_pdf(intro):
    """Описание данных на с. 4 указывает payload_min_c как 5 °C.

    В данных он равен -5.0. Хардкод по документу дал бы ложные отказы
    thermal_limit в диапазоне -5..+5 °C, где работает нагреватель.
    """
    assert intro["model"]["payload_min_c"] == -5.0
    assert intro["model"]["heater_below_c"] == 5.0


def test_downlink_job_has_single_executor(shift):
    for job in shift["jobs"]:
        if job["kind"] == "downlink":
            assert len(job["eligible_satellites"]) == 1


# ------------------------------------------------------------------ энергия

def test_energy_per_step_matches_hand_calculation(intro):
    sat = next(v for v in intro["satellites"] if v["downlink_w"] == 90.0)
    model = intro["model"]
    # 90 Вт * 300 с / 3600 / 0.95 = 7.8947 Вт*ч
    value = energy_per_step_wh(sat, model, "downlink", 300)
    assert round(value, 4) == 7.8947


def test_reserve_blocks_action_before_and_after(intro):
    scenario = copy.deepcopy(intro)
    for sat in scenario["satellites"]:
        sat["initial_soc_pct"] = 29.0  # ниже резерва 30%
    env = Environment(scenario)
    sid = scenario["satellites"][0]["id"]
    ok, reason, _ = env.can_execute(sid, {"action": "calibrate"})
    assert not ok and reason == "energy_reserve"


# ------------------------------------------------------------------ калибровка

def test_expired_calibration_blocks_jobs(intro):
    """Порядок проверок важен: no_contact срабатывает раньше calibration_required.

    Поэтому проверяем на шаге, где контакт заведомо есть — иначе тест
    поймает не ту причину и ничего не докажет.
    """
    scenario = copy.deepcopy(intro)
    valid = scenario["model"]["calibration_valid_steps"]
    for sat in scenario["satellites"]:
        sat["initial_calibration_age_steps"] = valid
        sat["initial_soc_pct"] = 90.0

    # Ищем пару (задание, шаг) внутри окна, где канал доступен.
    target = None
    for job in scenario["jobs"]:
        sid = job["eligible_satellites"][0]
        row = scenario["environment"][sid][job["kind"] + "_available"]
        for k in range(job["release_step"], job["deadline_step"]):
            if row[k]:
                target = (job, sid, k)
                break
        if target:
            break
    assert target, "в сценарии не нашлось задания с доступным каналом"

    job, sid, step = target
    env = Environment(scenario)
    while env.k < step:
        env.step({})
    ok, reason, _ = env.can_execute(sid, {"action": "job", "job_id": job["id"]})
    assert not ok and reason == "calibration_required"


def test_calibration_age_grows_during_outage(intro):
    """Во время недоступности возраст калибровки продолжает расти.

    Значит аппарат может выйти из отказа уже просроченным.
    """
    scenario = copy.deepcopy(intro)
    sid = scenario["satellites"][0]["id"]
    scenario["failures"] = [{"satellite_id": sid, "start_step": 0, "end_step": 5}]
    env = Environment(scenario)
    before = env.state[sid]["calibration_age_steps"]
    for _ in range(5):
        env.step({})
    assert env.state[sid]["calibration_age_steps"] == before + 5


# ------------------------------------------------------------------ сроки

def test_completion_exactly_on_deadline_counts(intro):
    """Последняя операция может завершиться ровно на границе срока."""
    scenario = copy.deepcopy(intro)
    job = next(j for j in scenario["jobs"] if j["work_steps"] == 1)
    env = Environment(scenario)
    sid = job["eligible_satellites"][0]
    last = job["deadline_step"] - 1
    while env.k < last:
        env.step({})
    ok, _, _ = env.can_execute(sid, {"action": "job", "job_id": job["id"]})
    if ok:
        env.step({sid: {"action": "job", "job_id": job["id"]}})
        assert env.jobs[job["id"]]["completed_step"] == job["deadline_step"]


# ------------------------------------------------------------------ планировщики

@pytest.mark.parametrize("planner_cls", [BaselinePlanner, EconomicPlanner])
def test_planner_never_exceeds_ground_capacity(intro, planner_cls):
    """Планировщик обязан сам согласовывать назначения (описание данных, с. 8)."""
    session = Session(copy.deepcopy(intro))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = planner_cls()
    cfg = PlannerConfig()
    while session.env.k < index.steps:
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})
    forbidden = {"ground_capacity", "duplicate_job_in_step"}
    offenders = [r for r in session.env.trace if r["reason"] in forbidden]
    assert not offenders, f"конфликты, которые должен был снять планировщик: {offenders[:3]}"


@pytest.mark.parametrize("planner_cls", [BaselinePlanner, EconomicPlanner])
def test_planner_protects_the_ground_link(shift, planner_cls):
    """На шагах с единственным каналом шлюз не уходит на калибровку."""
    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = planner_cls()
    cfg = PlannerConfig()
    violations = []
    for _ in range(60):
        step = session.env.k
        gateway = select_gateway(session, index, step)
        decisions = planner.decide(session, index, cfg)
        if gateway and len(index.contacts(step)) == 1:
            if decisions[gateway].action.get("action") == "calibrate":
                violations.append((step, gateway))
        session.advance({sid: d.action for sid, d in decisions.items()})
    assert not violations, f"шлюз отправлен на калибровку: {violations[:3]}"


# ------------------------------------------------------------------ события

def test_event_must_arrive_at_current_step(intro):
    session = Session(copy.deepcopy(intro))
    event = {"id": "E1", "at_step": 5, "type": "satellite_outage",
             "satellite_ids": [intro["satellites"][0]["id"]], "end_step": 10}
    with pytest.raises(ValueError):
        session.apply_event(event)


def test_rejected_event_does_not_touch_state(intro):
    session = Session(copy.deepcopy(intro))
    session.advance({})
    before = session.state_digest()
    bad = {"id": "E-BAD", "at_step": 1, "type": "add_jobs", "jobs": [{"id": "X"}]}
    with pytest.raises(ValueError):
        session.apply_event(bad)
    assert session.state_digest() == before


def test_duplicate_event_id_rejected(intro):
    session = Session(copy.deepcopy(intro))
    sid = intro["satellites"][0]["id"]
    event = {"id": "E1", "at_step": 0, "type": "satellite_outage",
             "satellite_ids": [sid], "end_step": 4}
    session.apply_event(event)
    with pytest.raises(ValueError):
        session.apply_event(dict(event))


# ------------------------------------------------------------------ ветвление

def test_fork_starts_from_identical_state(intro):
    session = Session(copy.deepcopy(intro))
    for _ in range(4):
        session.advance({})
    forked = session.fork()
    assert forked.state_digest() == session.state_digest()
    forked.advance({})
    assert forked.state_digest() != session.state_digest()


# ------------------------------------------------------------------ выгрузка

def test_export_replays_to_the_same_summary(intro):
    session = Session(copy.deepcopy(intro))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = BaselinePlanner()
    cfg = PlannerConfig()
    for _ in range(12):
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})
    result = session.result()
    replayed = replay_episode(result["initial_scenario"], result["events"],
                              result["commands"], result["steps_executed"])
    assert replayed.summary() == result["summary"]


# ------------------------------------------------------------------ оценка

def test_tightness_detects_provable_infeasibility(shift):
    index = ScenarioIndex.build(shift)
    job = {"id": "IMPOSSIBLE", "kind": "downlink", "release_step": 0,
           "deadline_step": 5, "work_steps": 5, "priority": 3, "value_usd": 100.0,
           "eligible_satellites": [index.sat_ids[0]], "remaining_steps": 5}
    assessment = valuation.assess(index, job, 0)
    if assessment.opportunities < 5:
        assert assessment.provably_infeasible
        assert assessment.reason in ("infeasible_by_contacts", "infeasible_by_deadline")


def test_cost_card_is_computed_before_execution(intro):
    index = ScenarioIndex.build(intro)
    job = intro["jobs"][0]
    card = build_cost_card(index, job["eligible_satellites"][0], job)
    assert card.energy_wh > 0
    assert card.sat_steps == job["work_steps"]
    assert card.ground_slots == (job["work_steps"] if job["kind"] == "downlink" else 0)


def test_preflight_finds_structural_oversubscription(shift):
    report = preflight.analyse(shift)
    assert report["ground_link"]["steps_with_single_gateway"] > 0
    assert report["capacity"]["downlink"]["capacity_steps"] > 0
    assert report["money"]["reachable_ceiling_usd"] <= report["money"]["total_usd"]


# ------------------------------------------------------------------ автономия

def test_federated_respects_global_limits_without_coordination(shift):
    """Борта не договариваются на лету, значит конфликтов быть не должно.

    Лимит наземного сегмента раскладывается по бортам заранее, на Земле,
    а задания в бандлах не пересекаются.
    """
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = FederatedPlanner(horizon_steps=48, replan_gap=4)
    cfg = PlannerConfig(mode="federated")
    for _ in range(80):
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})
    forbidden = {"ground_capacity", "duplicate_job_in_step"}
    assert not [r for r in session.env.trace if r["reason"] in forbidden]


def test_bundles_are_uploaded_only_in_contact_windows(intro):
    """P01_intro: связи нет 85% смены, разрыв до 37 шагов подряд.

    Загрузка полётного задания возможна только там, где контакт есть.
    """
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(intro))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = FederatedPlanner(horizon_steps=48, replan_gap=1)
    cfg = PlannerConfig(mode="federated")
    while session.env.k < index.steps:
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})

    upload_steps = [entry["step"] for entry in planner.log.entries]
    assert upload_steps, "ни одной загрузки не случилось"
    for step in upload_steps:
        assert index.contacts(step), f"загрузка на шаге {step} без связи с Землёй"


def test_state_is_preserved_through_the_longest_blackout(intro):
    """Что на самом деле показывает слепой участок P01_intro (З8.5).

    Прежняя версия проверяла ``executed != "idle"`` и проходила — но
    проходила за счёт КАЛИБРОВКИ, а не выполнения заданий. На шагах, где
    у всех аппаратов недоступны оба канала, ``can_execute`` вернёт
    ``no_contact`` для любого задания: выполнять там нечего по модели.

    Проверяется то, что действительно верно: заданий на слепых шагах нет
    ни одного, а состояние продолжает считаться — заряд, температура,
    возраст калибровки, прогресс заданий.
    """
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(intro))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = FederatedPlanner(horizon_steps=48, replan_gap=1)
    cfg = PlannerConfig(mode="federated")
    while session.env.k < index.steps:
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})

    env = index.env
    blind = [k for k in range(index.steps)
             if not any(env[sid]["downlink_available"][k]
                        or env[sid]["relay_available"][k]
                        for sid in index.sat_ids)]
    assert blind, "в сценарии нет слепых шагов — тест проверял бы пустоту"
    assert max(len(list(group)) for group in _runs(blind)) >= 30,         "ожидается длинный непрерывный разрыв"

    trace = session.env.trace
    on_blind = [r for r in trace if r["step"] in blind]

    # 1. Задания на слепых шагах невозможны — и ни одного не исполнено.
    assert not [r for r in on_blind if r["executed"] == "job"]

    # 2. Состояние продолжает считаться: обслуживание идёт, возраст
    #    калибровки и физика не замирают.
    assert any(r["executed"] == "calibrate" for r in on_blind)
    assert any(r["energy_after_wh"] != r["energy_before_wh"] for r in on_blind)
    assert any(r["temp_after_c"] != r["temp_before_c"] for r in on_blind)

    # 3. Прогресс заданий, начатых до разрыва, не сброшен.
    first_blind = min(blind)
    started_before = {r["completed_job"] for r in trace
                      if r["step"] < first_blind and r["completed_job"]}
    for job_id in started_before:
        assert session.env.jobs[job_id]["completed_step"] is not None


def _runs(steps: list[int]):
    """Непрерывные отрезки в возрастающем списке шагов."""
    import itertools
    for _, group in itertools.groupby(enumerate(steps),
                                      key=lambda pair: pair[1] - pair[0]):
        yield [value for _, value in group]


def test_branch_gets_its_own_bundles(intro):
    """Ветви независимы: копия полётных заданий своя."""
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(intro))
    index = ScenarioIndex.build(session.initial_scenario)
    planner = FederatedPlanner(horizon_steps=48, replan_gap=4)
    cfg = PlannerConfig(mode="federated")
    for _ in range(6):
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})

    clone = copy.deepcopy(planner)
    assert clone.bundles is not planner.bundles
    for sid, bundle in planner.bundles.items():
        assert clone.bundles[sid] is not bundle


# ------------------------------------------------------------------ вытеснение

def test_urgent_expensive_job_preempts_cheaper_work(shift):
    """З2: срочная дорогая заявка прерывает менее ценную работу.

    Событие подаёт задание приоритета 3 стоимостью $300 при медиане $16.66.
    Ожидается не просто его выполнение, а именно вытеснение: в разборе
    решения появляется причина ``preempted_by`` с указанием, кем именно.
    """
    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective="commercial")
    planner = EconomicPlanner()

    seen: list[dict] = []
    for _ in range(96):
        step = session.env.k
        if step == 40:
            sid = index.sat_ids[0]
            event = {
                "id": "PREEMPT-1", "at_step": step, "type": "add_jobs",
                "jobs": [{"id": "URGENT-RICH", "kind": "relay",
                          "release_step": step, "deadline_step": step + 6,
                          "work_steps": 3, "priority": 3, "value_usd": 300.0,
                          "eligible_satellites": list(index.sat_ids[:3])}],
            }
            session.apply_event(event)
            for job in event["jobs"]:
                index.register(job)
            assert sid  # задание доступно нескольким исполнителям
        decisions = planner.decide(session, index, cfg)
        for decision in decisions.values():
            preempted = decision.explain.get("preempted")
            if preempted:
                seen.append(preempted)
        session.advance({s: d.action for s, d in decisions.items()})

    assert seen, "вытеснение ни разу не сработало — механизм не подключён"
    assert all(p["reason"] == "preempted_by" for p in seen)
    assert all(p["by_job_id"] and p["gain"] > 0 for p in seen)
    assert session.env.jobs["URGENT-RICH"]["completed_step"] is not None


def test_preemption_respects_hysteresis(shift):
    """Гистерезис гасит вытеснение туда-обратно на соседних шагах."""
    def count_switches(frac: float) -> int:
        session = Session(copy.deepcopy(shift))
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = PlannerConfig(objective="commercial",
                            preemption_hysteresis_frac=frac)
        planner = EconomicPlanner()
        previous: dict[str, str] = {}
        switches = 0
        for _ in range(120):
            decisions = planner.decide(session, index, cfg)
            for sid, decision in decisions.items():
                job_id = decision.action.get("job_id")
                was = previous.get(sid)
                # Смена задания до его завершения — это и есть «дёрганье».
                if was and job_id and job_id != was \
                        and session.env.jobs[was]["completed_step"] is None:
                    switches += 1
                previous[sid] = job_id or previous.get(sid, "")
            session.advance({s: d.action for s, d in decisions.items()})
        return switches

    assert count_switches(2.0) <= count_switches(0.0)


# ------------------------------------------------------------------ назначение

class _FakeVerdict:
    """Минимальный вердикт для проверки чистой логики назначения."""

    def __init__(self, sid: str, job_id: str, net_value: float) -> None:
        self.satellite_id = sid
        self.job_id = job_id
        self.net_value = net_value


def test_hungarian_beats_greedy_on_the_classic_conflict():
    """З4: случай, где жадность теряет, а двудольное назначение — нет.

    S1 может взять только дорогое J1. S2 может взять J1 или чуть менее
    дорогое J2. Жадность отдаёт J1 второму аппарату (он первый по NV),
    и первый остаётся без работы.
    """
    from backend.flow.dispatch import assign_greedy, assign_optimal

    verdicts = [
        _FakeVerdict("S1", "J1", 100.0),
        _FakeVerdict("S2", "J1", 101.0),
        _FakeVerdict("S2", "J2", 90.0),
    ]
    kind_of = lambda job_id: "relay"  # noqa: E731

    greedy = assign_greedy(verdicts, downlink_limit=2, kind_of=kind_of)
    optimal = assign_optimal(verdicts, downlink_limit=2, kind_of=kind_of)

    assert sum(v.net_value for v in greedy.values()) == 101.0
    assert sum(v.net_value for v in optimal.values()) == 190.0
    assert {sid: v.job_id for sid, v in optimal.items()} == {"S1": "J1", "S2": "J2"}


def test_optimal_assignment_respects_the_ground_limit():
    """Лимит наземного сегмента соблюдается и при оптимальном назначении."""
    from backend.flow.dispatch import assign_optimal

    verdicts = [_FakeVerdict(f"S{i}", f"D{i}", 100.0 - i) for i in range(5)]
    plan = assign_optimal(verdicts, downlink_limit=2,
                          kind_of=lambda job_id: "downlink")
    assert len(plan) == 2
    # Остаются самые ценные.
    assert {v.job_id for v in plan.values()} == {"D0", "D1"}


def test_hungarian_matches_a_brute_force_optimum():
    """Проверка самого алгоритма перебором на малых матрицах."""
    import itertools
    import random

    from backend.flow.dispatch import hungarian

    random.seed(7)
    for _ in range(40):
        rows = random.randint(1, 5)
        cols = random.randint(1, 6)
        matrix = [[random.uniform(-10, 10) for _ in range(cols)]
                  for _ in range(rows)]
        mine = sum(matrix[i][j]
                   for i, j in enumerate(hungarian(matrix)) if j >= 0)
        take = min(rows, cols)
        best = min(
            sum(matrix[i][j] for i, j in zip(picked_rows, permutation))
            for picked_rows in itertools.combinations(range(rows), take)
            for columns in itertools.combinations(range(cols), take)
            for permutation in itertools.permutations(columns)
        )
        assert mine == pytest.approx(best, abs=1e-9)


# ------------------------------------------------------------------ память

def test_old_decisions_are_trimmed_but_explain_still_answers():
    """З6: журнал решений усечён, но разбор доступен по любому шагу."""
    from backend import config
    from backend.core.registry import REGISTRY
    from backend.flow.explain import decision_report

    run = REGISTRY.create("P02_shift", PlannerConfig(objective="commercial"),
                          "economic")
    run.advance(config.DECISIONS_FULL_STEPS + 20)
    sid = run.index.sat_ids[0]
    old_step = 3
    fresh_step = run.step - 1

    assert run.decisions[old_step][sid]["explain"]["truncated"] is True
    assert "truncated" not in run.decisions[fresh_step][sid]["explain"]

    # Быстрый путь: панель разбора открывается без пересчёта. Кандидаты
    # и теневые цены хранятся всегда, поэтому показывать уже есть что.
    quick = decision_report(run, old_step, sid)
    assert "recomputed" not in quick["considered"]
    assert quick["detail_available"] is False
    assert "top_candidates" in quick["considered"]
    assert quick["requested"] == run.decisions[old_step][sid]["action"]

    # Глубокая часть — по отдельному запросу, это повтор прогона.
    report = decision_report(run, old_step, sid, full=True)
    assert report["considered"]["recomputed"] is True
    assert report["detail_available"] is True
    # Пересчёт обязан совпасть с историей, иначе разбор врёт.
    assert report["requested"] == run.decisions[old_step][sid]["action"]
    assert report["reason_code"] == run.decisions[old_step][sid]["reason_code"]
    REGISTRY.drop(run.id)


def test_recomputed_decision_reproduces_events():
    """Пересчёт учитывает сообщения: без них история разойдётся."""
    from backend.core.registry import REGISTRY
    from backend.flow.explain import decision_report

    run = REGISTRY.create("P02_shift", PlannerConfig(objective="commercial"),
                          "economic")
    run.advance(40)
    run.apply_event({
        "id": "MEM-1", "at_step": run.step, "type": "add_jobs",
        "jobs": [{"id": "MEM-JOB", "kind": "relay", "release_step": run.step,
                  "deadline_step": run.step + 8, "work_steps": 2,
                  "priority": 3, "value_usd": 250.0,
                  "eligible_satellites": list(run.index.sat_ids[:3])}]})
    target = run.step
    run.advance(60)

    restored = run.recompute_decision(target)
    assert restored is not None
    for sid, decision in restored.items():
        assert decision["action"] == run.decisions[target][sid]["action"]
    REGISTRY.drop(run.id)


# ------------------------------------------------- временная корректность (З7)

def test_future_job_is_not_used_before_release(shift):
    """Будущее не используется заранее: задание до выпуска невидимо.

    Проверяется не только отсутствие команды, но и то, что планировщик
    вообще не считает его кандидатом: иначе оно исказило бы теневые цены.
    """
    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective="commercial")
    planner = EconomicPlanner()

    late = min((j for j in session.env.jobs.values() if j["release_step"] > 10),
               key=lambda j: j["release_step"])
    for _ in range(int(late["release_step"])):
        decisions = planner.decide(session, index, cfg)
        for decision in decisions.values():
            assert decision.action.get("job_id") != late["id"]
            seen = {c["job_id"] for c in decision.explain.get("top_candidates", [])}
            assert late["id"] not in seen
        session.advance({s: d.action for s, d in decisions.items()})

    assert session.env.k == late["release_step"]
    index.refresh(session.env.k, session.env.jobs)
    assert late["id"] in index.active, "после выпуска задание обязано появиться"


def test_event_is_not_applied_twice_even_across_steps(intro):
    """Повторное сообщение отклоняется и не портит состояние."""
    from backend.core.registry import REGISTRY
    from backend.errors import EventRejected

    run = REGISTRY.create("P01_intro", PlannerConfig(), "economic")
    run.advance(3)
    event = {"id": "DUP-X", "at_step": run.step, "type": "add_jobs",
             "jobs": [{"id": "DUP-JOB", "kind": "relay", "release_step": run.step,
                       "deadline_step": run.step + 10, "work_steps": 1,
                       "priority": 1, "value_usd": 10.0,
                       "eligible_satellites": [run.index.sat_ids[0]]}]}
    run.apply_event(event)
    digest = run.session.state_digest()

    with pytest.raises(EventRejected):
        run.apply_event(copy.deepcopy(event))
    assert run.session.state_digest() == digest
    assert len(run.events) == 1

    run.advance(2)
    with pytest.raises(EventRejected):
        run.apply_event(copy.deepcopy(event))
    REGISTRY.drop(run.id)


def test_branches_are_independent_after_divergence():
    """Ветви не влияют друг на друга: разные цели дают разные истории."""
    from backend.core.registry import REGISTRY

    parent = REGISTRY.create("P02_shift", PlannerConfig(objective="commercial"),
                             "economic")
    parent.advance(40)
    digest_at_branch = parent.session.state_digest()

    left = REGISTRY.branch(parent.id, PlannerConfig(objective="commercial"),
                           "economic")
    right = REGISTRY.branch(parent.id,
                            PlannerConfig(objective="priority_service"),
                            "economic")
    assert left.session.state_digest() == digest_at_branch
    assert right.session.state_digest() == digest_at_branch

    left.advance(40)
    # Родитель и вторая ветвь не сдвинулись от работы первой.
    assert parent.step == 40
    assert right.step == 40
    assert parent.session.state_digest() == digest_at_branch

    right.advance(40)
    assert left.session.summary() != right.session.summary()
    assert parent.step == 40

    for run in (parent, left, right):
        REGISTRY.drop(run.id)


# ------------------------------------------------------------------ допуск

def test_admission_does_not_discount_value_by_urgency(shift):
    """З1: срочность влияет на очередь, но не на решение «брать ли».

    Именно смешение этих двух вопросов ломало коммерческий режим: выручка
    уценивалась множителем срочности, а цена ресурсов — нет, и прибыльная
    работа отклонялась как убыточная.
    """
    from backend.flow import admission
    from backend.flow.shadow_prices import ShadowPrices

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    job = next(j for j in session.env.jobs.values()
               if j["release_step"] == 0 and j["kind"] == "relay"
               and j["deadline_step"] > j["work_steps"] * 4)
    sid = job["eligible_satellites"][0]
    prices = ShadowPrices(ground_slot=0.0,
                          energy_wh={s: 0.0 for s in index.sat_ids},
                          sat_step={s: 0.0 for s in index.sat_ids})

    verdict = admission.evaluate(index, session, sid, job, 0, prices,
                                 "commercial")
    assert verdict.admitted
    # margin считается по полной стоимости задания...
    assert verdict.margin == pytest.approx(float(job["value_usd"]))
    # ...а net_value уценён срочностью и потому строго меньше.
    assert verdict.detail["urgency"] < 1.0
    assert verdict.net_value < verdict.margin


def test_admission_refuses_work_the_energy_cannot_finish(shift):
    """Задание не начинается, если прогноз заряда его не покрывает."""
    from backend.flow import admission, energy
    from backend.flow.shadow_prices import ShadowPrices

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    forecast = energy.build(index, session, 0, 12)
    sid = index.sat_ids[0]
    prices = ShadowPrices(ground_slot=0.0,
                          energy_wh={s: 0.0 for s in index.sat_ids},
                          sat_step={s: 0.0 for s in index.sat_ids})
    job = {"id": "HUNGRY", "kind": "relay", "release_step": 0,
           "deadline_step": 6, "work_steps": 6, "priority": 1,
           "value_usd": 500.0, "eligible_satellites": [sid],
           "remaining_steps": 6}

    # Прогноз обещает меньше, чем нужно, — значит браться нельзя.
    if not forecast.covers(sid, "relay", 6):
        verdict = admission.evaluate(index, session, sid, job, 0, prices,
                                     "commercial", forecast=forecast)
        assert not verdict.admitted
        assert verdict.reason in ("energy_forecast_short",
                                  "infeasible_by_contacts",
                                  "infeasible_by_deadline")


# -------------------------------------------------- правки внешнего аудита (З8)

def test_capacity_is_counted_by_contact_windows(shift):
    """З8.1: ёмкость — по окнам связи, потолок отдаётся отдельно."""
    report = preflight.analyse(shift)
    downlink = report["capacity"]["downlink"]
    relay = report["capacity"]["relay"]

    index = ScenarioIndex.build(shift)
    limit = int(shift["model"]["downlink_parallel_limit"])
    expected = sum(min(limit, len(index.contacts(k))) for k in range(index.steps))

    assert downlink["contact_capacity"] == expected
    assert downlink["theoretical_ceiling"] == limit * index.steps
    # Потолок обязан быть строго больше: иначе расписание контактов полное
    # и вся правка теряет смысл — значит данные не те, что мы проверяли.
    assert downlink["theoretical_ceiling"] > downlink["contact_capacity"]
    assert downlink["capacity_steps"] == downlink["contact_capacity"]
    assert relay["theoretical_ceiling"] > relay["contact_capacity"]
    # Переподписка по окнам строже, чем по потолку.
    assert downlink["oversubscription"] > downlink["oversubscription_theoretical"]


def test_zero_value_critical_job_still_wins_in_priority_mode(shift):
    """З8.2: приоритет 3 не зависит от стоимости задания.

    В выданных данных заданий с value_usd = 0 нет, но схема их допускает,
    и жюри может подать свой сценарий. Множитель стоимости такую заявку
    просто обнулял бы; лексикографическое сравнение — нет.
    """
    from backend.flow import admission
    from backend.flow.shadow_prices import ShadowPrices

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    prices = ShadowPrices(ground_slot=0.0,
                          energy_wh={s: 0.0 for s in index.sat_ids},
                          sat_step={s: 0.0 for s in index.sat_ids})
    sid = index.sat_ids[0]
    base = {"kind": "relay", "release_step": 0, "deadline_step": 40,
            "work_steps": 2, "eligible_satellites": [sid], "remaining_steps": 2}
    free_critical = {**base, "id": "FREE-P3", "priority": 3, "value_usd": 0.0}
    rich_ordinary = {**base, "id": "RICH-P1", "priority": 1, "value_usd": 999.0}

    critical = admission.evaluate(index, session, sid, free_critical, 0, prices,
                                  "priority_service")
    ordinary = admission.evaluate(index, session, sid, rich_ordinary, 0, prices,
                                  "priority_service")

    assert critical.admitted, "бесплатное задание приоритета 3 должно допускаться"
    assert critical.tier > ordinary.tier
    assert critical.beats(ordinary)
    # Порядок сортировки планировщика ставит его первым.
    assert sorted([ordinary, critical], key=lambda v: v.sort_key)[0] is critical

    # В коммерческом режиме приоритет не даёт преимущества — деньги решают.
    commercial_critical = admission.evaluate(index, session, sid, free_critical,
                                             0, prices, "commercial")
    commercial_ordinary = admission.evaluate(index, session, sid, rich_ordinary,
                                             0, prices, "commercial")
    assert commercial_critical.tier == commercial_ordinary.tier == 0
    assert commercial_ordinary.beats(commercial_critical)


def test_critical_job_preempts_ordinary_regardless_of_price(shift):
    """З8.2: разряд сравнивается раньше денег и при вытеснении."""
    from backend.flow import admission

    cheap_critical = admission.Verdict("S01", "P3-CHEAP", True, 1.0, 1.0,
                                       "accepted", {}, tier=1)
    rich_ordinary = admission.Verdict("S01", "P1-RICH", True, 900.0, 900.0,
                                      "accepted", {}, tier=0)

    assert admission.preemption_gain(rich_ordinary, cheap_critical,
                                     work_done_steps=2, price_per_step=50.0,
                                     hysteresis=100.0) > 0
    assert admission.preemption_gain(cheap_critical, rich_ordinary,
                                     work_done_steps=0, price_per_step=0.0) < 0


def test_cost_card_separates_marginal_from_full_load(intro):
    """З8.3: карта отдаёт предельные затраты и полную нагрузку раздельно."""
    from backend.flow.cost import full_load_energy_per_step_wh

    index = ScenarioIndex.build(intro)
    job = next(j for j in intro["jobs"] if j["kind"] == "downlink")
    sid = job["eligible_satellites"][0]
    card = build_cost_card(index, sid, job)
    sat = index.sats[sid]

    marginal = energy_per_step_wh(sat, intro["model"], "downlink",
                                  intro["time"]["step_s"])
    full = full_load_energy_per_step_wh(sat, intro["model"], "downlink",
                                        intro["time"]["step_s"])

    assert card.marginal_energy_per_step_wh == pytest.approx(marginal, abs=1e-3)
    assert card.full_load_energy_per_step_wh == pytest.approx(full, abs=1e-3)
    # Полная нагрузка больше предельной ровно на служебное потребление.
    assert full > marginal
    assert (full - marginal) == pytest.approx(
        sat["base_w"] * intro["time"]["step_s"] / 3600
        / intro["model"]["discharge_efficiency"], abs=1e-6)
    # Старые имена сохранены: интерфейс не ломается.
    assert card.energy_per_step_wh == card.marginal_energy_per_step_wh
    assert card.energy_wh == card.marginal_energy_wh


def test_gateway_protection_never_loses_the_link_to_calibration(shift):
    """З8.4: политика делает ровно то, что обещает.

    Гарантия — не «шлюз всегда свободен» (аппарат, ведущий downlink, как
    раз и передаёт на Землю), а «единственный канал не уходит на
    калибровку». Проверяется именно это.
    """
    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective="commercial", protect_gateway=True)
    planner = EconomicPlanner()

    lost = 0
    for _ in range(120):
        step = session.env.k
        contacts = [s for s in index.contacts(step) if session.env.available(s)]
        decisions = planner.decide(session, index, cfg)
        if contacts and all(decisions[s].action.get("action") == "calibrate"
                            for s in contacts):
            lost += 1
        session.advance({s: d.action for s, d in decisions.items()})

    assert lost == 0


# ------------------------------------------------- кэш бандлов на Земле (З9)

def test_uplink_cache_does_not_change_a_single_decision(shift):
    """Кэш обязан быть невидим для борта: та же смена до последнего бита.

    Земля передаёт только разницу, но борт получает ровно тот же бандл.
    Если это не так, кэш меняет расчёт — а значит и учёт, и выгрузку.
    """
    from backend.planners.federated import FederatedPlanner

    def play(cache: bool):
        session = Session(copy.deepcopy(shift))
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = PlannerConfig(mode="federated", replan_gap_steps=4)
        planner = FederatedPlanner(48, 4, cache_uplink=cache)
        actions = []
        for _ in range(120):
            decisions = planner.decide(session, index, cfg)
            actions.append({sid: d.action for sid, d in decisions.items()})
            session.advance(actions[-1])
        return actions, session.state_digest(), session.summary(), planner

    with_cache, digest_on, summary_on, planner_on = play(True)
    without, digest_off, summary_off, planner_off = play(False)

    assert with_cache == without, "кэш изменил команды"
    assert digest_on == digest_off
    assert summary_on == summary_off
    # Бандлы на борту совпадают, а не только итог.
    assert ({sid: b.to_dict() for sid, b in planner_on.bundles.items()}
            == {sid: b.to_dict() for sid, b in planner_off.bundles.items()})


def test_uplink_cache_actually_saves_volume(shift):
    """Экономия положительная — иначе кэш только мешает.

    Проверка не формальная: до перехода на «проводной» вид элемента
    (BundleItem.wire_dict) кэш давал ОТРИЦАТЕЛЬНУЮ экономию, потому что
    backup_release_step менялся у каждого элемента при каждой загрузке.
    """
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(mode="federated", replan_gap_steps=4)
    planner = FederatedPlanner(48, 4, cache_uplink=True)
    for _ in range(144):
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})

    volume = planner.log.volume()
    assert volume["uploads"] > 1, "одной загрузки мало, чтобы кэш что-то дал"
    assert volume["bytes_sent"] < volume["bytes_without_cache"]
    assert volume["saved_share"] > 0.3, volume
    assert volume["items_sent"] < volume["items_without_cache"]


def test_wire_format_drops_only_derived_fields(shift):
    """В эфир не уходит то, что борт может вычислить сам.

    priority_rank — это индекс в очереди, backup_release_step считается
    из issued_at_step и backup_delay, которые есть в заголовке.
    """
    from backend.autonomy import bundle as bundle_mod

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    ranked = [j["id"] for j in list(session.env.jobs.values())[:5]]
    built = bundle_mod.build(index, session, index.sat_ids[0], 0, 48, ranked,
                             backup_delay=2)

    wire = built.wire_dict()
    assert wire["backup_delay"] == 2
    assert wire["issued_at_step"] == 0

    full_fields = set(built.queue[0].to_dict())
    wire_fields = set(wire["queue"][0])
    assert full_fields - wire_fields == {"priority_rank", "backup_release_step"}

    # Выброшенное действительно восстанавливается из заголовка и порядка.
    for rank, (item, sent) in enumerate(zip(built.queue, wire["queue"])):
        assert sent["job_id"] == item.job_id
        assert rank == item.priority_rank
        restored = wire["issued_at_step"] + wire["backup_delay"] * sent["backup_rank"]
        assert restored == item.backup_release_step

    # Полный вид для интерфейса не обеднел: поля на месте.
    assert "priority_rank" in built.to_dict()["queue"][0]
    assert "backup_release_step" in built.to_dict()["queue"][0]


def test_uplink_cache_never_makes_the_message_bigger(shift):
    """Кэш обязан быть не хуже полной передачи при любом интервале.

    При редкой загрузке очередь успевает обновиться целиком: совпадений
    нет, а список снятых идёт сверху, и дельта выходит длиннее полного
    сообщения. На replan_gap = 48 «экономия» была −9.3%, поэтому Земля
    берёт меньшее из двух представлений.
    """
    from backend.planners.federated import FederatedPlanner

    for gap in (4, 48):
        session = Session(copy.deepcopy(shift))
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = PlannerConfig(mode="federated", replan_gap_steps=gap)
        planner = FederatedPlanner(48, gap, cache_uplink=True)
        while session.env.k < index.steps:
            decisions = planner.decide(session, index, cfg)
            session.advance({sid: d.action for sid, d in decisions.items()})

        volume = planner.log.volume()
        assert volume["bytes_sent"] <= volume["bytes_without_cache"], gap
        assert volume["saved_share"] >= 0.0, (gap, volume)


# --------------------------------- признак «не окупится» в бандле (ЭТАП 2)

def test_speculative_flag_marks_work_beyond_board_capacity():
    """Земля помечает то, что не влезает в ёмкость борта на горизонте.

    Проверяется на P04_demand: там спрос втрое выше ёмкости, и часть
    заданий физически не может быть отработана тем бортом, которому
    досталась. Такой элемент борт не начинает.
    """
    from backend.planners.federated import FederatedPlanner

    session = Session(copy.deepcopy(STORE.get("P04_demand").scenario))
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective="commercial", mode="federated",
                        replan_gap_steps=4)
    planner = FederatedPlanner(48, 4)

    skipped: list[dict] = []
    for _ in range(48):
        decisions = planner.decide(session, index, cfg)
        for decision in decisions.values():
            skipped += [x for x in decision.explain.get("skipped", [])
                        if x["reason"] == "speculative"]
        session.advance({sid: d.action for sid, d in decisions.items()})

    flagged = [item for bundle in planner.bundles.values()
               for item in bundle.queue if item.speculative]
    assert flagged, "в перегруженном сценарии признак обязан срабатывать"
    assert skipped, "признак не повлиял ни на одно решение"

    # Признак едет на борт: борт не может его вычислить сам.
    wire = planner.bundles[index.sat_ids[0]].wire_dict()
    assert all("speculative" in item for item in wire["queue"])


def test_speculative_work_is_finished_once_started(shift):
    """Начатое добивается, даже если помечено спекулятивным.

    Бросить начатое — гарантированно потерять вложенные шаги, поэтому
    признак запрещает только НАЧИНАТЬ.
    """
    from backend.autonomy import bundle as bundle_mod
    from backend.autonomy.onboard_agent import decide_onboard

    session = Session(copy.deepcopy(shift))
    index = ScenarioIndex.build(session.initial_scenario)
    job = min((j for j in session.env.jobs.values()
               if j["kind"] == "relay" and j["work_steps"] >= 2),
              key=lambda j: j["release_step"])
    # Исполнителя берём из самого задания: у произвольного аппарата его
    # может не быть в списке допустимых.
    sid = next(s for s in job["eligible_satellites"] if s in index.sats)

    # Доходим до шага выпуска: до него задание вообще недоступно.
    while session.env.k < job["release_step"]:
        session.advance({s: IDLE for s in index.sat_ids})
    step = session.env.k

    built = bundle_mod.build(index, session, sid, step, 48, [job["id"]],
                             speculative={job["id"]})
    assert built.queue[0].speculative

    # Не начатое — пропускается.
    first = decide_onboard(session, index, sid, built, 3)
    assert first.action.get("job_id") != job["id"]
    assert any(x["reason"] == "speculative"
               for x in first.explain.get("skipped", []))

    # То же задание, но уже начатое — берётся в работу.
    job["remaining_steps"] = job["work_steps"] - 1
    resumed = decide_onboard(session, index, sid, built, 3)
    assert resumed.action.get("job_id") == job["id"]
    assert resumed.reason_code == "accepted"


def test_speculative_never_blocks_a_critical_job_in_priority_mode():
    """Приоритет 3 в приоритетном режиме не помечается спекулятивным.

    Обязательство сильнее расчёта ёмкости — то же исключение, что в
    admission.evaluate. Иначе режимы противоречили бы друг другу.
    """
    from backend.planners.federated import FederatedPlanner

    for objective, expect_critical_flagged in (("priority_service", False),
                                               ("commercial", True)):
        session = Session(copy.deepcopy(STORE.get("P04_demand").scenario))
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = PlannerConfig(objective=objective, mode="federated",
                            replan_gap_steps=4)
        planner = FederatedPlanner(48, 4)
        for _ in range(24):
            decisions = planner.decide(session, index, cfg)
            session.advance({sid: d.action for sid, d in decisions.items()})

        critical_flagged = any(
            int(session.env.jobs[item.job_id]["priority"]) == 3
            for bundle in planner.bundles.values()
            for item in bundle.queue if item.speculative)
        assert critical_flagged == expect_critical_flagged, objective


def test_speculative_flag_saves_energy_and_money_under_overload():
    """Признак обязан улучшать и деньги, и расход энергии.

    Если он только экономит энергию за счёт выручки — он не нужен:
    выручка и есть цель. Замер на перегруженном сценарии.
    """
    from backend.planners.federated import FederatedPlanner

    def play(skip: bool):
        session = Session(copy.deepcopy(STORE.get("P04_demand").scenario))
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = PlannerConfig(objective="priority_service", mode="federated",
                            replan_gap_steps=12)
        planner = FederatedPlanner(48, 12, skip_speculative=skip)
        while session.env.k < index.steps:
            decisions = planner.decide(session, index, cfg)
            session.advance({sid: d.action for sid, d in decisions.items()})

        model = index.model
        spent = 0.0
        for row in session.env.trace:
            if row["executed"] != "job":
                continue
            sat = index.sats[row["satellite_id"]]
            kind = session.env.jobs[row["requested"]["job_id"]]["kind"]
            spent += (sat[kind + "_w"] * index.step_s / 3600
                      / model["discharge_efficiency"])
        return session.summary(), spent

    off, spent_off = play(False)
    on, spent_on = play(True)

    assert on["revenue_usd"] > off["revenue_usd"]
    assert on["critical_jobs_completed_on_time"] >= off["critical_jobs_completed_on_time"]
    assert on["work_steps_in_missed_jobs"] < off["work_steps_in_missed_jobs"]
    assert spent_on < spent_off, "энергии должно тратиться меньше"
