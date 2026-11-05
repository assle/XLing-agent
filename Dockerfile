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

FROM base AS runtime
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# 应用在导入风险评估模块时就需要这些依赖，不能只安装在测试镜像中。
RUN --mount=type=cache,target=/root/.cache/pip pip install torch==2.13.0 --find-links https://mirrors.aliyun.com/pytorch-wheels/cpu \
    && pip install transformers==4.46.3 peft==0.13.2

FROM runtime AS test
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN --mount=type=cache,target=/root/.cache/pip pip install pytest==9.1.1
COPY evals ./evals
COPY finetune ./finetune
COPY docker-compose.yml ./docker-compose.yml
CMD ["python", "-m", "pytest", "-q"]

# 默认构建保持为应用运行镜像；测试依赖和文件只进入显式的 test 目标。
FROM runtime AS release
