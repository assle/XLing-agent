FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY app ./app
COPY models/xling-qwen2.5-7b-ft/Modelfile ./models/xling-qwen2.5-7b-ft/Modelfile

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM base AS test
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN --mount=type=cache,target=/root/.cache/pip pip install torch==2.13.0 --find-links https://mirrors.aliyun.com/pytorch-wheels/cpu \
    && pip install pytest==9.1.1 transformers==4.46.3 peft==0.13.2
COPY evals ./evals
COPY finetune ./finetune
COPY docker-compose.yml ./docker-compose.yml
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
