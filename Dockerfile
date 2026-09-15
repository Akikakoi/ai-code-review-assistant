FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs

RUN pip install --upgrade pip && pip install .

# 非 root 运行；沙箱相关能力由 worker 侧的 Docker 提供，本镜像不需要特权
RUN useradd --create-home --uid 1000 acra \
    && mkdir -p /var/lib/acra/work && chown -R acra:acra /var/lib/acra
USER acra

ENV ACRA_WORKDIR=/var/lib/acra/work

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8000/healthz',timeout=3).status_code==200 else 1)"

CMD ["acra", "serve", "--host", "0.0.0.0", "--port", "8000"]
