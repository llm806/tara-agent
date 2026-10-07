FROM python:3.12-slim-bookworm AS build
WORKDIR /app
# 固定安装工具版本，构建只依赖 Docker Hub 和 Python 包仓库。
RUN pip install --no-cache-dir uv==0.12.21
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 tara && useradd --uid 10001 --gid tara --no-create-home tara \
    && mkdir -p /data/processed && chown tara:tara /data/processed
COPY --from=build /app/.venv ./.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY deploy ./deploy
# 后端以普通用户运行，原始数据通过只读目录挂载。
USER tara
EXPOSE 8000
ENTRYPOINT ["python", "deploy/start_backend.py"]
