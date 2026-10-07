"""候选功能分析的只读 MCP 薄层，计算与范围规则由分析服务承担。"""

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from tara_agent.analysis.function_atlas import MatouAtlasService
from tara_agent.analysis.function_atlas_models import (
    AtlasQuery,
    AtlasResult,
    ComparisonResult,
    FunctionComparisonQuery,
    SequenceQuery,
    SequenceResult,
)
from tara_agent.analysis.function_query_models import (
    FunctionProfileQuery,
    FunctionSamplesQuery,
    FunctionSamplesResult,
)
from tara_agent.analysis.function_service import MatouFunctionService
from tara_agent.analysis.function_signal_models import FunctionSignalResult
from tara_agent.analysis.function_study import FunctionStudyService
from tara_agent.analysis.function_study_models import FunctionStudyQuery, FunctionStudyResult
from tara_agent.mcp.tools import READ_ONLY


def register_function_tools(server: MCPServer, service: MatouFunctionService) -> None:
    atlas = MatouAtlasService(service.reader)
    study = FunctionStudyService(service.reader)
    scope = (
        f"当前数据：{service.reader.manifest.dataset_version}，"
        f"已准备类群：{service.reader.manifest.selection['taxon']}。"
    )

    @server.tool(
        title="硅藻功能谱、粒径海区分布与环境PLS分析",
        annotations=READ_ONLY,
        description=scope
        + (
            "对应论文图9a/9b/9c和10b/10e，默认Top100；合并核糖体、泛素等作者功能类别。"
            "current_data计算当前候选数据并附作者参考排名；paper_reference独立重算作者公开汇总表。"
            "明确返回分母、来源、图号及每项完成状态；缺少LHC亚家族预计算时保留已完成结果。"
            "需在MATOU目录内准备paper_task2资料包；不得把作者参考结果冒充当前数据。"
        ),
    )
    def function_study(query: FunctionStudyQuery) -> FunctionStudyResult:
        try:
            return study.analyze(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        title="查询候选功能分析样本",
        annotations=READ_ONLY,
        description=scope + "列出 MetaG 或 MetaT 原始样本名；不提供 DNA/RNA 配对或环境映射。",
    )
    def find_function_samples(
        query: Annotated[
            FunctionSamplesQuery,
            Field(
                description="实验类型必填；类群须属于已准备的数据范围。按原样本名检索，不解释名称。",
            ),
        ],
    ) -> FunctionSamplesResult:
        """列出已准备类群的 MetaG 或 MetaT 原始样本名；不提供 DNA/RNA 配对或环境映射。"""
        try:
            return service.find_samples(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        title="分析跨样本功能谱与总体 Top Pfam",
        annotations=READ_ONLY,
        description=scope
        + (
            "按原始样本等权统计已观测候选 Pfam 贡献并返回样本功能谱；"
            "默认分析所选实验全部样本。分母包括未注释基因，不宣称绝对表达。"
            "超过20个样本须先准备匹配 E 条件的缓存。"
        ),
    )
    def function_atlas(query: AtlasQuery) -> AtlasResult:
        try:
            return atlas.atlas(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        title="比较目标功能的 MetaG 与 MetaT 相对信号",
        annotations=READ_ONLY,
        description=scope
        + (
            "使用版本化作者编码规则精确对应站位、水层、批次与原始过滤组的 DNA11/cDNA14；"
            "返回相对贡献、差值、描述性 Spearman rho、IQR 与总体 CV。"
            "排除 WGA、近似粒径、未对应样本；不证明同一提取物，"
            "不计算 RNA/DNA 活性或显著性。默认使用全部合格对应编码。"
        ),
    )
    def compare_function_signals(query: FunctionComparisonQuery) -> ComparisonResult:
        try:
            return atlas.compare(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        title="查询 MATOU 候选基因核酸序列",
        annotations=READ_ONLY,
        description=scope
        + (
            "仅查询提前从 MATOU FASTA 完整核验标题映射并准备的序列；"
            "按 geneID/Pfam 筛选，分页返回精确标题和核酸序列。"
            "未准备时明确报错；不运行任意文件读取或序列预测。"
        ),
    )
    def retrieve_gene_sequences(query: SequenceQuery) -> SequenceResult:
        try:
            return atlas.sequences(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        title="计算单样本候选功能谱",
        annotations=READ_ONLY,
        description=scope
        + (
            "计算一个指定样本内候选 Pfam 的数值和与已提供类群记录内份额。"
            "不补零、不推断单位、绝对表达、跨样本可比性或 RNA/DNA 比值；"
            "统一 E-value 条件不是家族官方阈值。"
        ),
    )
    def function_profile(
        query: Annotated[
            FunctionProfileQuery,
            Field(
                description=(
                    "指定 MetaG/MetaT 和准确样本名，可选择 Pfam、Top N 及候选 E-value 条件。"
                ),
            ),
        ],
    ) -> FunctionSignalResult:
        """计算一个样本内候选 Pfam 数值和与已提供类群记录内份额。

        不补零、不推断单位、绝对表达、跨样本可比性或 RNA/DNA 比值。
        统一 E-value 条件不是家族官方阈值。
        """
        try:
            return service.function_profile(query)
        except ValueError as error:
            raise ToolError(str(error)) from error
