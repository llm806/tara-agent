"""Tara 查询与分析服务的轻量 MCP 适配层。"""

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from tara_agent.analysis.compute import TaraComputeService
from tara_agent.analysis.compute_models import (
    DiversityQuery,
    DiversityResult,
    EnvironmentAssociationQuery,
    EnvironmentAssociationResult,
    TaxonAbundanceQuery,
    TaxonAbundanceResult,
)
from tara_agent.analysis.models import (
    FindSamplesQuery,
    FindSamplesResult,
    FindTaxaQuery,
    FindTaxaResult,
    SampleInfoResult,
)
from tara_agent.analysis.service import AnalysisNotFoundError, TaraQueryService
from tara_agent.data.catalog import source_filename
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def register_tools(
    server: MCPServer,
    *,
    query_service: TaraQueryService,
    compute_service: TaraComputeService,
) -> None:
    """注册六个 MVP 工具，不增加业务逻辑。"""

    @server.tool(title="查询 Tara 样本", annotations=READ_ONLY)
    def find_samples(
        query: Annotated[
            FindSamplesQuery,
            Field(
                description="受限的样本筛选条件。",
                examples=[{"ocean_region_contains": "Mediterranean", "limit": 20}],
            ),
        ],
    ) -> FindSamplesResult:
        """使用明确且受限的环境条件查询样本背景信息。"""

        return _call(
            "TaraQueryService.find_samples",
            {"query": query.model_dump(mode="json")},
            lambda: query_service.find_samples(query),
        )

    @server.tool(title="获取 Tara 样本信息", annotations=READ_ONLY)
    def get_sample_info(
        sample_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description="准确的 PANGAEA 样本 ID。",
                examples=["TARA_A100000005"],
            ),
        ],
    ) -> SampleInfoResult:
        """根据准确的 PANGAEA 样本 ID 获取完整且已校验的背景信息。"""

        return _call(
            "TaraQueryService.get_sample_info",
            {"sample_id": sample_id},
            lambda: query_service.get_sample_info(sample_id),
        )

    @server.tool(title="查询 Tara 分类单元", annotations=READ_ONLY)
    def find_taxa(
        query: Annotated[
            FindTaxaQuery,
            Field(
                description="针对指定标记的分类学查询。",
                examples=[{"marker": "v4", "taxon": "Bacillariophyta"}],
            ),
        ],
    ) -> FindTaxaResult:
        """在指定标记中查询匹配的 ASV 及其样本出现情况。"""

        return _call(
            "TaraQueryService.find_taxa",
            {"query": query.model_dump(mode="json")},
            lambda: query_service.find_taxa(query),
        )

    @server.tool(title="计算分类单元丰度", annotations=READ_ONLY)
    def taxon_abundance(
        query: Annotated[
            TaxonAbundanceQuery,
            Field(
                description="针对指定标记的丰度计算请求。",
                examples=[{"marker": "v4", "taxon": "Bacillariophyta"}],
            ),
        ],
    ) -> TaxonAbundanceResult:
        """计算类群测序相对丰度。order_by=relative_abundance 在全集计算后排序再分页。

        group_by=station 返回站点等权样本均值、最大值及两套排名，aggregation 控制列表排序。
        taxonomic_rank=genus/species/asv 返回 1–20 个指定样本内所选类群组成，
        每样本独立排序分页；包括全样本相对丰度和类群内部占比及优势类群摘要。
        未鉴定记录保留，属种不是凭名称猜测；均值和最大值不表示绝对或细胞丰度。
        """

        return _call(
            "TaraComputeService.taxon_abundance",
            {"query": query.model_dump(mode="json")},
            lambda: compute_service.taxon_abundance(query),
        )

    @server.tool(title="计算 Tara Alpha 多样性", annotations=READ_ONLY)
    def diversity_analysis(
        query: Annotated[
            DiversityQuery,
            Field(
                description="针对指定标记的 Alpha 多样性计算请求。",
                examples=[{"marker": "v9", "group_by": "polar"}],
            ),
        ],
    ) -> DiversityResult:
        """计算未稀释抽样的观测 ASV 丰富度与 Shannon 多样性。"""

        return _call(
            "TaraComputeService.diversity_analysis",
            {"query": query.model_dump(mode="json")},
            lambda: compute_service.diversity_analysis(query),
        )

    @server.tool(title="分析 Tara 环境关联", annotations=READ_ONLY)
    def environment_association(
        query: Annotated[
            EnvironmentAssociationQuery,
            Field(
                description="一个分类单元和一个允许使用的环境变量。",
                examples=[
                    {
                        "marker": "v4",
                        "taxon": "Bacillariophyta",
                        "environment_variable": "temperature",
                    }
                ],
            ),
        ],
    ) -> EnvironmentAssociationResult:
        """计算一次基于成对完整观测值的 Spearman 丰度关联。"""

        return _call(
            "TaraComputeService.environment_association",
            {"query": query.model_dump(mode="json")},
            lambda: compute_service.environment_association(query),
        )


def _call[ResultT](
    name: str,
    input_data: dict[str, Any],
    operation: Callable[[], ResultT],
) -> ResultT:
    """仅暴露模型可以纠正的错误，其他故障不对外公开。"""

    try:
        with observe(
            name,
            ObservationKind.SERVICE,
            ObservationUpdate(input_data=input_data),
        ) as observation:
            result = operation()
            observation.finish(_result_details(result))
            return result
    except (AnalysisNotFoundError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _result_details(result: Any) -> ObservationUpdate:
    output = result.model_dump(mode="json")
    metadata = output.get("metadata", {})
    provenance = metadata.get("provenance", {})
    source_datasets = provenance.get("source_datasets", [])
    marker = provenance.get("marker")
    sample_count = provenance.get("sample_count")
    return ObservationUpdate(
        output_data=output,
        filters=provenance.get("filters") or None,
        marker=str(marker) if marker is not None else None,
        sample_count=sample_count if isinstance(sample_count, int) else None,
        data_sources=[
            {"filename": source_filename(str(dataset))}
            for dataset in source_datasets
        ],
    )
