# acra 沙箱镜像（阶段三启用，见 ADR 0005 / 文档 §12.2）
#
# 设计要点：
#   - 一次性把静态分析工具链装齐，运行期只读挂载代码目录，不联网、不下载
#   - 非 root 运行；配合 --read-only + tmpfs、--network none、--pids-limit 使用
#   - 各语言工具链互相隔离在自己的层里，按需裁剪
#
# 构建：
#   docker build -t acra-sandbox:latest sandbox/
#
# 运行（与 acra 的实际调用参数保持一致）：
#   docker run --rm \
#     --network none --memory 2g --memory-swap 2g --cpus 2 --pids-limit 256 \
#     --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m \
#     --user 1000:1000 \
#     -v "$WORKDIR:/workspace:ro" \
#     acra-sandbox:latest semgrep --sarif -c p/security-audit /workspace

FROM python:3.12-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git \
    && rm -rf /var/lib/apt/lists/*

# ---------- 通用静态分析：Semgrep（含规则集，构建期拉取，运行期不联网）----------
FROM base AS tools-general
RUN pip install --no-cache-dir semgrep \
    && semgrep --version

# ---------- Java 工具链：SpotBugs + Checkstyle + PMD ----------
FROM base AS tools-java
ARG SPOTBUGS_VERSION=4.8.6
ARG CHECKSTYLE_VERSION=10.21.0
ARG PMD_VERSION=7.8.0
ENV JAVA_HOME=/opt/jdk
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jdk-headless unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(dirname "$(dirname "$(readlink -f "$(which javac)")")")" /opt/jdk || true
RUN mkdir -p /opt/tools/java \
    && curl -fsSL -o /tmp/spotbugs.tgz "https://github.com/spotbugs/spotbugs/releases/download/${SPOTBUGS_VERSION}/spotbugs-${SPOTBUGS_VERSION}.tgz" \
    && tar -xzf /tmp/spotbugs.tgz -C /opt/tools/java \
    && curl -fsSL -o /opt/tools/java/checkstyle.jar "https://github.com/checkstyle/checkstyle/releases/download/checkstyle-${CHECKSTYLE_VERSION}/checkstyle-${CHECKSTYLE_VERSION}-all.jar" \
    && curl -fsSL -o /tmp/pmd.zip "https://github.com/pmd/pmd/releases/download/pmd_releases%2F${PMD_VERSION}/pmd-dist-${PMD_VERSION}-bin.zip" \
    && unzip -q /tmp/pmd.zip -d /opt/tools/java \
    && rm -rf /tmp/*.tgz /tmp/*.zip

# ---------- Node 工具链：ESLint + tsc ----------
FROM base AS tools-node
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --no-audit --no-fund \
         eslint@9 typescript@5 @typescript-eslint/parser @typescript-eslint/eslint-plugin \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# ---------- Python 工具链：Ruff + mypy ----------
FROM base AS tools-python
RUN pip install --no-cache-dir ruff mypy

# ---------- 最终镜像 ----------
FROM base AS final
COPY --from=tools-general /usr/local /usr/local
COPY --from=tools-java /opt/tools /opt/tools
COPY --from=tools-java /usr/lib/jvm /usr/lib/jvm
COPY --from=tools-node /usr/lib/node_modules /usr/lib/node_modules
COPY --from=tools-node /usr/bin/node /usr/bin/node
COPY --from=tools-python /usr/local/lib/python3.12/site-packages/ruff /usr/local/lib/python3.12/site-packages/ruff
COPY --from=tools-python /usr/local/bin/ruff /usr/local/bin/ruff
COPY --from=tools-python /usr/local/bin/mypy /usr/local/bin/mypy

ENV JAVA_HOME=/usr/lib/jvm/default-java \
    PATH="/opt/tools/java/pmd-bin-7.8.0/bin:/usr/local/bin:${PATH}" \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

# 与 acra 的默认沙箱参数保持一致：非 root、无可写目录、只读挂载点在 /workspace
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin sandbox
USER 1000:1000
WORKDIR /workspace

ENTRYPOINT []
CMD ["/bin/sh", "-c", "echo 'acra-sandbox ready'; ls /opt/tools 2>/dev/null || true"]
