"""Точка входа сервиса.

    uvicorn backend.app.main:app --reload
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.app.api import router
from backend.errors import ServiceError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cosmo")

app = FastAPI(
    title="Автономное управление потоком заданий спутниковой группировки",
    version=config.APP_VERSION,
    description="КосмоХакатон 2026. Сервис принимает поток коммерческих заданий, "
                "оценивает каждое по деньгам и срочности, считает ресурсную "
                "себестоимость до запуска и решает, какие брать в работу.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # dev-режим; на деплое сузить до своего домена
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Отказ по схеме запроса — в том же формате, что и остальные ошибки.

    Без этого FastAPI отвечает своим {"detail": ...} по-английски, и
    интерфейсу приходится разбирать два разных формата ошибок вместо
    одного. Разбор pydantic сохраняется в details: он нужен разработчику.
    """
    problems = []
    for item in exc.errors():
        where = ".".join(str(part) for part in item.get("loc", ())[1:])
        problems.append({"field": where or "тело запроса",
                         "message": item.get("msg", ""),
                         "type": item.get("type", "")})
    first = problems[0]["field"] if problems else "тело запроса"
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "VALIDATION_FAILED",
            "message": (f"Запрос не соответствует схеме: поле {first!r}"
                        if problems else "Не удалось разобрать тело запроса"),
            "details": {"problems": problems},
        }},
    )


@app.exception_handler(Exception)
async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    """Необработанное исключение не должно повреждать состояние расчётов."""
    log.exception("Необработанная ошибка")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": "Внутренняя ошибка сервиса",
                           "details": {"type": type(exc).__name__}}},
    )


@app.get("/api/health")
def health() -> dict:
    from backend.core.scenario_store import STORE
    return {"status": "ok", "version": config.APP_VERSION,
            "scenarios": len(STORE.list())}


app.include_router(router)

# Собранный фронтенд раздаётся тем же процессом — один образ, один порт.
_static = config.ROOT / "frontend" / "dist"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="frontend")
