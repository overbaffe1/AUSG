# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- зависимости
# Отдельная стадия: слой с пакетами не пересобирается при правке кода.
FROM python:3.11-slim AS backend-deps
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-dev.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# ---------------------------------------------------------------- разработка
# Код НЕ копируется: он пробрасывается томом из docker-compose.
# Правка файла — и uvicorn перезапускается сам, пересборка не нужна.
FROM backend-deps AS backend-dev
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/backend", "--reload-dir", "/app/model"]

# ---------------------------------------------------------------- сборка фронта
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- боевой образ
FROM backend-deps AS runtime
WORKDIR /app
COPY model/ ./model/
COPY backend/ ./backend/
COPY data/ ./data/
COPY examples/ ./examples/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
EXPOSE 5017
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "5017"]
