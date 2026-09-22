"""存活状态与就绪状态接口。"""

from fastapi import APIRouter, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tara_agent import __version__
from tara_agent.api.schemas import DatasetStatus, HealthResponse
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.persistence import Database

router = APIRouter(tags=["service"])


async def _health_response(request: Request) -> HealthResponse:
    reader: ProcessedDataReader | None = request.app.state.data_reader
    database: Database | None = request.app.state.database
    settings = request.app.state.settings

    datasets: list[DatasetStatus] = []
    if reader is not None:
        for key, source in reader.manifest.sources.items():
            sample_count = 0
            if key == "18s_v4":
                sample_count = reader.manifest.coverage.v4_samples
            elif key == "18s_v9":
                sample_count = reader.manifest.coverage.v9_samples
            datasets.append(
                DatasetStatus(
                    key=key,
                    filename=source.filename,
                    ready=True,
                    exists=True,
                    size_bytes=source.size_bytes,
                    column_count=source.column_count,
                    sample_column_count=sample_count,
                    missing_columns=[],
                    duplicate_columns=[],
                    invalid_sample_columns=[],
                )
            )
    data_ready = reader is not None
    database_ready = await _database_ready(database)
    agent_ready = request.app.state.agent is not None
    persistence_required = settings.environment != "test"
    service_ready = (
        data_ready
        and agent_ready
        and (database_ready or not persistence_required)
    )
    return HealthResponse(
        status="ok" if service_ready else "degraded",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        data_ready=data_ready,
        database_ready=database_ready,
        agent_ready=agent_ready,
        model=settings.deepseek_model,
        datasets=datasets,
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return await _health_response(request)


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def ready(request: Request, response: Response) -> HealthResponse:
    payload = await _health_response(request)
    if payload.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


async def _database_ready(database: Database | None) -> bool:
    if database is None:
        return False
    try:
        await database.ping()
    except (OSError, SQLAlchemyError):
        return False
    return True
