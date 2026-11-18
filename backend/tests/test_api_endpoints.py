"""Проверка HTTP-слоя: все ручки раздела 7 ТЗ на реальных данных.

Проверяется не только «отвечает 200», но и то, что ответ можно
использовать: поля на месте, числа согласуются между ручками, ошибки
приходят в едином формате ``{"error": {"code", "message", "details"}}``,
а состояние расчёта после отказа не повреждено (критерий Т3).

Сценарий для большинства проверок — ``P01_intro``: 48 шагов, прогон
целиком занимает доли секунды. События проверяются на ``P02_shift``,
для которого написан ``examples/events_demo.json``.
"""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.app.main import app
from backend.core.registry import REGISTRY


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def run_id(client: TestClient):
    """Свежий расчёт P01_intro, удаляется после теста."""
    response = client.post("/api/runs", json={
        "scenario_id": "P01_intro", "objective": "commercial",
        "planner": "economic", "label": "проверка ручек"})
    assert response.status_code == 200, response.text
    rid = response.json()["run_id"]
    yield rid
    REGISTRY.drop(rid)


def error_of(response) -> dict:
    """Единый формат ошибки. Заодно проверяет, что он соблюдён."""
    payload = response.json()
    assert set(payload) == {"error"}, payload
    assert set(payload["error"]) == {"code", "message", "details"}, payload
    assert payload["error"]["message"], "сообщение об ошибке пустое"
    return payload["error"]


# ------------------------------------------------------------------ служебное

def test_health_reports_loaded_scenarios(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["version"] == config.APP_VERSION
    assert body["scenarios"] >= 4


def test_planners_lists_both_algorithms(client):
    names = {p["name"] for p in client.get("/api/planners").json()["planners"]}
    assert names == {"baseline", "economic"}
    assert all(p["version"] for p in client.get("/api/planners").json()["planners"])


def test_openapi_schema_is_served(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    # Все ручки, на которые опирается интерфейс, должны быть описаны.
    for path in ("/api/scenarios", "/api/runs", "/api/runs/{run_id}/advance",
                 "/api/runs/{run_id}/explain", "/api/runs/{run_id}/export",
                 "/api/runs/import", "/api/compare"):
        assert path in paths, f"{path} отсутствует в OpenAPI"


# ------------------------------------------------------------------ сценарии

def test_scenarios_list_and_detail(client):
    scenarios = client.get("/api/scenarios").json()["scenarios"]
    ids = {s["id"] for s in scenarios}
    assert {"P01_intro", "P02_shift", "P03_energy", "P04_demand"} <= ids

    detail = client.get("/api/scenarios/P02_shift").json()
    assert detail["id"] == "P02_shift"
    assert detail["steps"] == 288
    assert detail["jobs_total"] == 1208
    assert detail["jobs_by_kind"] == {"downlink": 344, "relay": 864}
    # Приоритет 3 показывается отдельно: это основной показатель режима.
    assert detail["jobs_by_priority"]["3"] == 334
    assert detail["value_total_usd"] > 0
    assert detail["duration_hours"] == 24.0


def test_unknown_scenario_returns_404_with_hint(client):
    response = client.get("/api/scenarios/НЕТ_ТАКОГО")
    assert response.status_code == 404
    error = error_of(response)
    assert error["code"] == "NOT_FOUND"
    # В деталях должен быть список известных — иначе оператору некуда идти.
    assert error["details"]


def test_preflight_reports_contact_capacity_not_ceiling(client):
    report = client.get("/api/scenarios/P04_demand/preflight").json()
    downlink = report["capacity"]["downlink"]
    relay = report["capacity"]["relay"]

    assert downlink["contact_capacity"] == 439
    assert downlink["theoretical_ceiling"] == 576
    assert relay["contact_capacity"] == 13728
    # Перегрузка спросом видна до запуска — это и есть смысл предполётной оценки.
    assert relay["oversubscription"] > 1.0
    assert report["ground_link"]["steps_with_single_gateway"] > 0
    # Шаги 224 и 230 — задокументированный разрыв связи.
    assert 224 in report["ground_link"]["blind_steps"]
    assert 230 in report["ground_link"]["blind_steps"]
    assert report["money"]["reachable_ceiling_usd"] <= report["money"]["total_usd"]


def test_job_cost_card_gives_marginal_and_full_load(client):
    from backend.core.scenario_store import STORE
    job_id = next(j["id"] for j in STORE.get("P02_shift").scenario["jobs"]
                  if j["kind"] == "downlink")

    body = client.get(f"/api/scenarios/P02_shift/jobs/{job_id}/cost").json()
    assert body["job"]["id"] == job_id
    cards = body["cost_cards"]
    assert cards, "ни одной карты стоимости"
    for card in cards:
        assert card["marginal_energy_wh"] > 0
        assert card["full_load_energy_wh"] > card["marginal_energy_wh"]
        assert card["energy_wh"] == card["marginal_energy_wh"]
        assert card["sat_steps"] == body["job"]["work_steps"]


def test_unknown_job_cost_returns_404(client):
    response = client.get("/api/scenarios/P02_shift/jobs/НЕТ/cost")
    assert response.status_code == 404
    assert error_of(response)["code"] == "NOT_FOUND"


def test_upload_accepts_valid_scenario_and_rejects_broken(client):
    from backend.core.scenario_store import STORE

    scenario = copy.deepcopy(STORE.get("P01_intro").scenario)
    scenario["meta"] = {**scenario["meta"], "id": "UPLOAD_CHECK",
                        "title": "Загруженный сценарий"}
    uploaded = client.post("/api/scenarios/upload", content=json.dumps(scenario))
    assert uploaded.status_code == 200, uploaded.text
    new_id = uploaded.json()["id"]
    assert client.get(f"/api/scenarios/{new_id}").status_code == 200

    # Битый JSON и чужая схема отклоняются, сервис остаётся живым.
    broken = client.post("/api/scenarios/upload", content="{не json")
    assert broken.status_code == 422
    assert error_of(broken)["code"] == "VALIDATION_FAILED"

    foreign = client.post("/api/scenarios/upload",
                          content=json.dumps({"schema_version": "чужая-1.0"}))
    assert foreign.status_code == 422
    assert client.get("/api/health").json()["status"] == "ok"


def test_derive_changes_only_allowed_initial_conditions(client):
    derived = client.post("/api/scenarios/P01_intro/derive", json={
        "solar_factor": 0.5, "title": "Половина солнца"})
    assert derived.status_code == 200, derived.text
    body = derived.json()
    assert body["derived_from"] == "P01_intro"
    assert body["derivation"]

    # Производный сценарий пригоден к расчёту наравне с встроенным.
    run = client.post("/api/runs", json={"scenario_id": body["id"]})
    assert run.status_code == 200
    REGISTRY.drop(run.json()["run_id"])

    # Пустой запрос и неизвестный аппарат отклоняются.
    assert client.post("/api/scenarios/P01_intro/derive", json={}).status_code == 422
    bad = client.post("/api/scenarios/P01_intro/derive",
                      json={"initial_soc_pct": {"НЕТ": 50}})
    assert bad.status_code == 422
    assert error_of(bad)["details"]


# ------------------------------------------------------------------ прогоны

def test_create_run_returns_usable_initial_state(client, run_id):
    state = client.get(f"/api/runs/{run_id}").json()
    assert state["step"] == 0
    assert state["finished"] is False
    assert state["objective"] == "commercial"
    assert state["planner"] == "economic"
    assert state["state_digest"]
    assert state["satellites"], "нет состояния аппаратов"
    for sid, sat in state["satellites"].items():
        assert 0 <= sat["soc_pct"] <= 100, sid
        assert sat["calibration_valid_for"] >= 0
    assert run_id in {r["run_id"] for r in client.get("/api/runs").json()["runs"]}


def test_create_run_rejects_unknown_planner_and_scenario(client):
    bad_planner = client.post("/api/runs", json={"scenario_id": "P01_intro",
                                                 "planner": "магия"})
    assert bad_planner.status_code == 422
    assert error_of(bad_planner)["details"]["known"] == ["baseline", "economic"]

    assert client.post("/api/runs", json={"scenario_id": "НЕТ"}).status_code == 404
    # Схема запроса тоже проверяется: цель ограничена двумя значениями.
    assert client.post("/api/runs", json={"scenario_id": "P01_intro",
                                          "objective": "любая"}).status_code == 422


def test_advance_by_steps_to_step_and_to_end(client, run_id):
    first = client.post(f"/api/runs/{run_id}/advance", json={"steps": 5}).json()
    assert first["state"]["step"] == 5
    assert first["executed_rows"] == 5 * len(first["state"]["satellites"])

    to_step = client.post(f"/api/runs/{run_id}/advance", json={"to_step": 20}).json()
    assert to_step["state"]["step"] == 20

    # Назад нельзя: выполненные шаги не пересчитываются.
    back = client.post(f"/api/runs/{run_id}/advance", json={"to_step": 10})
    assert back.status_code == 422
    assert back.json()["error"]["details"]["current_step"] == 20

    end = client.post(f"/api/runs/{run_id}/advance", json={"to_end": True}).json()
    assert end["state"]["finished"] is True
    assert end["state"]["step"] == end["state"]["steps_total"]

    # Завершённую смену нельзя двигать дальше.
    after = client.post(f"/api/runs/{run_id}/advance", json={"steps": 1})
    assert after.status_code == 409
    assert error_of(after)["code"] == "RUN_FINISHED"


def test_objective_change_is_recorded_and_applies_forward(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 10})
    changed = client.post(f"/api/runs/{run_id}/objective",
                          json={"objective": "priority_service"}).json()
    assert changed["objective"] == "priority_service"
    assert changed["history"] == [{"step": 10, "from": "commercial",
                                   "to": "priority_service"}]

    # Повторная установка той же цели историю не засоряет.
    again = client.post(f"/api/runs/{run_id}/objective",
                        json={"objective": "priority_service"}).json()
    assert len(again["history"]) == 1

    # Смена цели попадает в выгрузку — её увидит жюри.
    export = client.get(f"/api/runs/{run_id}/export",
                        params={"include_trace": False}).json()
    assert export["run_metadata"]["objective_changes"] == changed["history"]


def test_unknown_run_is_404_everywhere(client):
    for path in ("", "/flow", "/trace", "/analytics", "/bundles", "/export"):
        response = client.get(f"/api/runs/НЕТУ{path}")
        assert response.status_code == 404, path
        assert error_of(response)["code"] == "NOT_FOUND"
    assert client.post("/api/runs/НЕТУ/advance", json={"steps": 1}).status_code == 404
    assert client.delete("/api/runs/НЕТУ").status_code == 404


# ------------------------------------------------------------------ события

def test_event_from_the_jury_file_is_accepted_step_by_step(client):
    """Файл cosmo-B-events-1.0 целиком: применяется только то, что на текущем шаге."""
    events = json.loads((config.EXAMPLES_DIR / "events_demo.json")
                        .read_text(encoding="utf-8-sig"))
    run_id = client.post("/api/runs", json={
        "scenario_id": events["base_scenario"], "objective": "commercial"}).json()["run_id"]
    try:
        first_step = min(int(e["at_step"]) for e in events["events"])

        # На шаге 0 ни одно сообщение не применяется — будущее не берём заранее.
        early = client.post(f"/api/runs/{run_id}/events", json=events).json()
        assert early["accepted"] == []
        assert len(early["not_for_this_step"]) == len(events["events"])

        client.post(f"/api/runs/{run_id}/advance", json={"to_step": first_step})
        applied = client.post(f"/api/runs/{run_id}/events", json=events).json()
        assert applied["accepted"], "сообщение текущего шага не применилось"
        assert all(a["current_step"] == first_step for a in applied["accepted"])

        # Повтор того же сообщения отклоняется целиком, состояние цело.
        digest = client.get(f"/api/runs/{run_id}").json()["state_digest"]
        repeat = client.post(f"/api/runs/{run_id}/events", json=events)
        assert repeat.status_code == 409
        assert error_of(repeat)["code"] == "EVENT_REJECTED"
        assert client.get(f"/api/runs/{run_id}").json()["state_digest"] == digest

        # Смена продолжается из фактического состояния.
        assert client.post(f"/api/runs/{run_id}/advance",
                           json={"steps": 5}).status_code == 200
    finally:
        REGISTRY.drop(run_id)


def test_single_event_without_envelope_is_the_main_path(client, run_id):
    """Жюри подаёт один объект без обёртки — основной путь приёма."""
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 4})
    state = client.get(f"/api/runs/{run_id}").json()
    sids = sorted(state["satellites"])

    event = {"id": "API-ADD-1", "at_step": state["step"], "type": "add_jobs",
             "jobs": [{"id": "API-JOB-1", "kind": "relay",
                       "release_step": state["step"], "deadline_step": state["step"] + 10,
                       "work_steps": 2, "priority": 3, "value_usd": 120.0,
                       "eligible_satellites": sids[:2]}]}
    accepted = client.post(f"/api/runs/{run_id}/events", json=event)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted"] == [{"accepted": "API-ADD-1",
                                            "current_step": state["step"]}]

    # Новое задание видно в потоке и доступно планировщику.
    flow = client.get(f"/api/runs/{run_id}/flow").json()
    known = {j["job_id"] for bucket in flow["jobs"].values() for j in bucket}
    assert "API-JOB-1" in known

    # Сообщение не на текущем шаге не применяется и не ломает расчёт.
    stale = client.post(f"/api/runs/{run_id}/events",
                        json={**event, "id": "API-ADD-2", "at_step": state["step"] + 3})
    assert stale.status_code == 200
    assert stale.json()["accepted"] == []
    assert stale.json()["not_for_this_step"] == [{"id": "API-ADD-2",
                                                  "at_step": state["step"] + 3}]


def test_malformed_event_does_not_corrupt_the_run(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 3})
    before = client.get(f"/api/runs/{run_id}").json()

    # Тип есть, обязательных полей нет.
    broken = client.post(f"/api/runs/{run_id}/events",
                         json={"id": "BROKEN", "at_step": before["step"],
                               "type": "add_jobs"})
    assert broken.status_code == 409
    assert error_of(broken)["code"] == "EVENT_REJECTED"

    unknown_type = client.post(f"/api/runs/{run_id}/events",
                               json={"id": "STRANGE", "at_step": before["step"],
                                     "type": "полный_вздор"})
    assert unknown_type.status_code == 409

    after = client.get(f"/api/runs/{run_id}").json()
    assert after["state_digest"] == before["state_digest"]
    assert after["step"] == before["step"]
    assert client.post(f"/api/runs/{run_id}/advance",
                       json={"steps": 2}).status_code == 200


def test_manual_event_puts_satellite_out_of_service(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 2})
    state = client.get(f"/api/runs/{run_id}").json()
    sid = sorted(state["satellites"])[0]
    assert state["satellites"][sid]["available"] is True

    body = client.post(f"/api/runs/{run_id}/events/manual", json={
        "type": "satellite_outage", "satellite_ids": [sid],
        "end_step": state["step"] + 6}).json()
    assert body["event"]["at_step"] == state["step"]
    assert body["state"]["satellites"][sid]["available"] is False

    # Возраст калибровки продолжает расти во время недоступности.
    age_before = body["state"]["satellites"][sid]["calibration_age_steps"]
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 4})
    after = client.get(f"/api/runs/{run_id}").json()["satellites"][sid]
    assert after["calibration_age_steps"] > age_before

    # Пустой список аппаратов не проходит схему.
    assert client.post(f"/api/runs/{run_id}/events/manual", json={
        "type": "satellite_outage", "satellite_ids": [],
        "end_step": 10}).status_code == 422


# ------------------------------------------------------------------ наблюдение

def test_flow_buckets_cover_every_job_exactly_once(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"to_end": True})
    flow = client.get(f"/api/runs/{run_id}/flow").json()

    total = sum(flow["counts"].values())
    scenario = client.get("/api/scenarios/P01_intro").json()
    assert total == scenario["jobs_total"], "задание потерялось или посчитано дважды"

    for job in flow["jobs"]["completed"]:
        assert job["completed_step"] is not None
        assert job["progress"] == 1.0
    for job in flow["jobs"]["waiting"]:
        assert job["remaining_steps"] == job["work_steps"]

    filtered = client.get(f"/api/runs/{run_id}/flow",
                          params={"status": "completed"}).json()
    assert set(filtered["jobs"]) == {"completed"}
    # Счётчики не зависят от фильтра — иначе UI покажет неверные итоги.
    assert filtered["counts"] == flow["counts"]

    bad = client.get(f"/api/runs/{run_id}/flow", params={"status": "чепуха"})
    assert bad.status_code == 422
    assert "known" in error_of(bad)["details"]


def test_trace_filters_and_paging(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 12})
    everything = client.get(f"/api/runs/{run_id}/trace",
                            params={"limit": 5000}).json()
    assert everything["returned"] == everything["total_rows"]

    sid = sorted(client.get(f"/api/runs/{run_id}").json()["satellites"])[0]
    by_sat = client.get(f"/api/runs/{run_id}/trace",
                        params={"satellite_id": sid, "limit": 5000}).json()
    assert by_sat["rows"], "по аппарату не нашлось ни строки"
    assert {r["satellite_id"] for r in by_sat["rows"]} == {sid}

    window = client.get(f"/api/runs/{run_id}/trace",
                        params={"from_step": 3, "to_step": 5}).json()
    assert {r["step"] for r in window["rows"]} <= {3, 4, 5}

    page = client.get(f"/api/runs/{run_id}/trace", params={"limit": 7}).json()
    assert page["returned"] == 7
    assert page["total_rows"] == everything["total_rows"]

    # Строка журнала обязана нести всё для разбора последствий.
    row = page["rows"][0]
    for field in ("step", "satellite_id", "requested", "executed", "reason",
                  "energy_before_wh", "energy_after_wh", "temp_after_c",
                  "load_w", "calibration_age_steps", "below_reserve"):
        assert field in row, field


def test_explain_answers_for_fresh_and_trimmed_steps(client, run_id):
    client.post(f"/api/runs/{run_id}/advance",
                json={"steps": config.DECISIONS_FULL_STEPS + 10})
    sid = sorted(client.get(f"/api/runs/{run_id}").json()["satellites"])[0]
    current = client.get(f"/api/runs/{run_id}").json()["step"]

    fresh = client.get(f"/api/runs/{run_id}/explain",
                       params={"step": current - 1, "satellite_id": sid}).json()
    assert fresh["reason_code"]
    assert fresh["reason_text"] != fresh["reason_code"], "код причины не расшифрован"
    assert "considered" in fresh and "consequences" in fresh
    assert fresh["mismatch"] is False or fresh["executed"] != fresh["requested"]["action"]

    # Старый шаг усечён ради памяти, но панель открывается мгновенно:
    # кандидаты и теневые цены хранятся всегда.
    old = client.get(f"/api/runs/{run_id}/explain",
                     params={"step": 1, "satellite_id": sid}).json()
    assert old["considered"].get("recomputed") is None
    assert old["detail_available"] is False
    assert old["requested"]["action"], "быстрый путь не вернул действие"

    # Глубокая часть восстанавливается пересчётом по отдельному запросу.
    deep = client.get(f"/api/runs/{run_id}/explain",
                      params={"step": 1, "satellite_id": sid, "full": True}).json()
    assert deep["considered"].get("recomputed") is True
    assert deep["detail_available"] is True
    assert deep["requested"] == old["requested"], "пересчёт разошёлся с историей"

    # Шаг, который ещё не выполнен, честно сообщает об этом, а не врёт.
    future = client.get(f"/api/runs/{run_id}/explain",
                        params={"step": current + 5, "satellite_id": sid}).json()
    assert future["available"] is False


def test_analytics_matches_the_run_summary(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"to_end": True})
    report = client.get(f"/api/runs/{run_id}/analytics").json()
    state = client.get(f"/api/runs/{run_id}").json()

    assert report["run_id"] == run_id
    assert report["summary"] == state["summary"]

    critical = report["priority_service"]
    if critical["critical_jobs_due"] == 0:
        # Нулевой знаменатель -> показателя нет, а не ноль процентов.
        assert critical["share"] is None
    else:
        assert 0.0 <= critical["share"] <= 1.0

    # Загрузка аппарата — доля шагов ЗАДАНИЙ; ожидание и калибровка
    # считаются отдельно (описание данных, с. 10).
    for sid, load in report["utilisation"].items():
        assert load["executed_steps"] == (load["job_steps"] + load["idle_steps"]
                                          + load["calibrate_steps"]), sid
        assert load["job_share"] == pytest.approx(
            load["job_steps"] / load["executed_steps"], abs=1e-4)

    link = report["ground_link"]
    assert 0.0 <= link["utilisation"] <= 1.0
    assert link["slots_used"] <= link["slots_available"]
    assert link["limit_per_step"] == 2

    # Планировщик разрешает конфликты сам: отказов библиотеки по общим
    # ресурсам быть не должно (описание данных, с. 8).
    blocked = report["blocked_reasons"]
    assert blocked.get("ground_capacity", 0) == 0
    assert blocked.get("duplicate_job_in_step", 0) == 0

    # У каждого простоя ровно одна причина из словаря REASONS.
    from backend.planners.base import REASONS
    assert set(report["idle_reasons"]) <= set(REASONS)


def test_bundles_are_empty_centralized_and_filled_federated(client, run_id):
    centralized = client.get(f"/api/runs/{run_id}/bundles").json()
    assert centralized["mode"] == "centralized"
    assert centralized["bundles"] == []
    assert centralized["note"]

    federated_id = client.post("/api/runs", json={
        "scenario_id": "P01_intro", "mode": "federated",
        "config": {"replan_gap_steps": 4}}).json()["run_id"]
    try:
        client.post(f"/api/runs/{federated_id}/advance", json={"steps": 20})
        body = client.get(f"/api/runs/{federated_id}/bundles").json()
        assert body["mode"] == "federated"
        assert body["replan_gap"] == 4
        assert body["bundles"], "бандлы не сформировались"
        assert body["uplink"]["uploads"], "нет ни одной загрузки"
        # Загрузка возможна только на шаге с контактом.
        for upload in body["uplink"]["uploads"]:
            assert upload["satellites"]
        for bundle in body["bundles"]:
            assert bundle["valid_until_step"] > bundle["issued_at_step"]
            for item in bundle["queue"]:
                assert isinstance(item["abandon_if"], dict)
                assert item["abandon_if"]["after_step"] > 0
                assert item["min_soc_pct"] >= 0
    finally:
        REGISTRY.drop(federated_id)


# ------------------------------------------------------------------ сравнение

def test_branch_is_independent_and_compare_gives_a_verdict(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 20})
    parent = client.get(f"/api/runs/{run_id}").json()

    branch = client.post(f"/api/runs/{run_id}/branch", json={
        "objective": "priority_service", "label": "ветвь приоритета"}).json()
    child_id = branch["run_id"]
    try:
        assert branch["step"] == parent["step"]
        assert branch["state_digest"] == parent["state_digest"]
        assert branch["objective"] == "priority_service"

        client.post(f"/api/runs/{child_id}/advance", json={"to_end": True})
        # Родитель не сдвинулся от работы ветви.
        assert client.get(f"/api/runs/{run_id}").json()["step"] == parent["step"]

        client.post(f"/api/runs/{run_id}/advance", json={"to_end": True})
        verdict = client.get("/api/compare",
                             params={"a": run_id, "b": child_id}).json()
        assert verdict["a"]["run_id"] == run_id
        assert verdict["b"]["run_id"] == child_id
        assert verdict["verdict"]["text"]
        assert "deltas" in verdict

        # Сравнение с несуществующим расчётом — 404, а не пустой ответ.
        assert client.get("/api/compare",
                          params={"a": run_id, "b": "НЕТ"}).status_code == 404
    finally:
        REGISTRY.drop(child_id)


# ------------------------------------------------------------------ выгрузка

def test_export_replays_through_the_library_and_import_confirms(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"to_end": True})

    full = client.get(f"/api/runs/{run_id}/export").json()
    assert full["schema_version"] == config.RESULT_SCHEMA
    assert full["run_metadata"]["algorithm"] == "economic"
    assert full["run_metadata"]["parameters"]["objective"] == "commercial"
    assert full["steps_executed"] == 48
    assert full["trace"], "полная выгрузка без журнала"

    light = client.get(f"/api/runs/{run_id}/export",
                       params={"include_trace": False}).json()
    assert "trace" not in light
    # Без журнала выгрузка обязана воспроизводиться так же.
    assert light["commands"] == full["commands"]

    confirmed = client.post("/api/runs/import", json=light).json()
    assert confirmed["reproduced"] is True, confirmed["mismatches"]
    assert confirmed["steps_executed"] == full["steps_executed"]
    assert confirmed["summary"] == full["summary"]


def test_import_rejects_foreign_schema_and_tampered_summary(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"steps": 20})
    result = client.get(f"/api/runs/{run_id}/export",
                        params={"include_trace": False}).json()

    foreign = client.post("/api/runs/import",
                          json={**result, "schema_version": "чужая-1.0"})
    assert foreign.status_code == 422
    assert error_of(foreign)["details"]["expected"] == config.RESULT_SCHEMA

    # Подделанные показатели ловятся: пересчёт не сходится с записанным.
    tampered = copy.deepcopy(result)
    tampered["summary"]["revenue_usd"] = 999999.0
    checked = client.post("/api/runs/import", json=tampered).json()
    assert checked["reproduced"] is False
    assert "revenue_usd" in checked["mismatches"]
    assert checked["mismatches"]["revenue_usd"]["saved"] == 999999.0

    # Испорченные команды не роняют сервис, а дают понятный отказ.
    broken = copy.deepcopy(result)
    broken["commands"] = [{"вздор": True}]
    response = client.post("/api/runs/import", json=broken)
    assert response.status_code in (422, 200)
    if response.status_code == 422:
        assert error_of(response)["code"] == "VALIDATION_FAILED"


def test_delete_run_removes_it_from_the_registry(client):
    rid = client.post("/api/runs", json={"scenario_id": "P01_intro"}).json()["run_id"]
    assert client.delete(f"/api/runs/{rid}").json() == {"deleted": rid}
    assert client.get(f"/api/runs/{rid}").status_code == 404
    assert rid not in {r["run_id"] for r in client.get("/api/runs").json()["runs"]}


# ------------------------------------------------------- единый формат ошибок

def test_schema_rejections_use_the_same_error_format(client):
    """Отказ по схеме запроса приходит в том же виде, что и остальные.

    Без обработчика RequestValidationError FastAPI отвечает своим
    {"detail": ...} по-английски, и интерфейсу пришлось бы разбирать два
    разных формата ошибок вместо одного.
    """
    cases = [
        ({}, "scenario_id"),
        ({"scenario_id": "P01_intro", "objective": "любая"}, "objective"),
        ({"scenario_id": "P01_intro", "mode": "какой-то"}, "mode"),
        ({"scenario_id": "P01_intro", "config": {"replan_gap_steps": 0}},
         "config.replan_gap_steps"),
        ({"scenario_id": 123}, "scenario_id"),
    ]
    for body, field in cases:
        response = client.post("/api/runs", json=body)
        assert response.status_code == 422, body
        error = error_of(response)
        assert error["code"] == "VALIDATION_FAILED"
        fields = {p["field"] for p in error["details"]["problems"]}
        assert field in fields, (body, fields)

    # Ручной ввод события проверяется так же.
    rid = client.post("/api/runs", json={"scenario_id": "P01_intro"}).json()["run_id"]
    try:
        bad = client.post(f"/api/runs/{rid}/events/manual",
                          json={"type": "выдумка", "satellite_ids": ["S01"],
                                "end_step": 5})
        assert bad.status_code == 422
        assert error_of(bad)["code"] == "VALIDATION_FAILED"
    finally:
        REGISTRY.drop(rid)


def test_trace_limit_is_capped_by_config(client, run_id):
    client.post(f"/api/runs/{run_id}/advance", json={"to_end": True})
    # Запрос за пределами разрешённого не валит сервис и не отдаёт больше
    # страницы: иначе один запрос по P04_demand вытянул бы десятки мегабайт.
    too_much = client.get(f"/api/runs/{run_id}/trace", params={"limit": 10**6})
    assert too_much.status_code == 200
    assert too_much.json()["returned"] <= config.TRACE_PAGE_LIMIT

    # Полный объём всё равно виден в total_rows — постранично его добрать можно.
    assert too_much.json()["total_rows"] >= too_much.json()["returned"]
    assert client.get(f"/api/runs/{run_id}/trace",
                      params={"limit": 0}).status_code == 422


def test_compare_requires_both_runs(client, run_id):
    assert client.get("/api/compare").status_code == 422
    assert client.get("/api/compare", params={"a": run_id}).status_code == 422
