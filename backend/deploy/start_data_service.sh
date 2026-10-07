#!/usr/bin/env bash
# 校内数据服务统一启动入口；凭据和数据独立于代码版本保存。
set -euo pipefail
cd "$(dirname "$0")/.."
export TARA_DATASET_DIR=/home/llm/data/TaraData
export TARA_PROCESSED_DATA_DIR=/home/llm/data/tara-agent/processed
export TMPDIR=/home/llm/data/tara-agent/tmp
export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-4}"
export TARA_DATA_SERVICE_TOKEN="$(cat /home/llm/apps/tara-agent/data-service.token)"
service_port="${TARA_DATA_SERVICE_PORT:-8010}"
/home/llm/.local/bin/uv sync --locked --python 3.12
exec .venv/bin/python -m uvicorn tara_agent.data_service.app:create_app \
  --factory --host 127.0.0.1 --port "$service_port" --workers 1
