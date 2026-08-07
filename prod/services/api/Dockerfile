FROM python:3.12.10-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/custombuild-venv \
    PATH=/opt/custombuild-venv/bin:$PATH \
    PYTHONPATH=/app/packages/domain/src:/app/packages/rule-engine/src:/app/packages/manufacturing/src:/app/packages/template-sdk/src:/app/cad/src:/app/cam/src:/app/postprocessors/src:/app/services/api:/app/services/worker

RUN useradd --create-home --uid 10001 custombuild
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.8.3 \
    && uv sync --locked --no-dev --no-install-project \
    && uv cache clean

COPY packages ./packages
COPY cad ./cad
COPY cam ./cam
COPY postprocessors ./postprocessors
COPY services/api ./services/api
COPY scripts ./scripts

USER custombuild
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
