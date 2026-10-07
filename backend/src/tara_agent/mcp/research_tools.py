"""群落与跨库关联工具薄层；数据映射和科学计算不在MCP内实现。"""

from mcp.server.mcpserver.exceptions import ToolError

from tara_agent.analysis.community import CommunityService
from tara_agent.analysis.community_models import (
    CommunityQuery,
    CommunityResult,
    FunctionEnvironmentQuery,
    FunctionEnvironmentResult,
)
from tara_agent.analysis.function_environment import FunctionEnvironmentService
from tara_agent.mcp.tools import READ_ONLY


def register_research_tools(server, reader, matou_reader, research):
    community = CommunityService(reader, research)

    @server.tool(
        title="群落分布、多样性及环境关系",
        annotations=READ_ONLY,
        description=(
            "按所选类群、V4/V9、样本和输出目标分析群落。默认硅藻双标记，"
            "原样本ASV总读数≥3、出现≥2样本，全部真核reads分母；"
            "先计算Shannon/expShannon再汇总重复及等价粒径。输出全球分布和纬度数据、"
            "Spearman/BH、两成分PLS、四粒径Bray–Curtis/NMDS stress及环境拟合。"
            "outputs可只选择请求产物；paper环境来自离线作者资料包，"
            "context_stat只分析其可用变量。缺资料仅阻断依赖项目，不补零或猜映射。"
        ),
    )
    def community_analysis(query: CommunityQuery) -> CommunityResult:
        try:
            return community.analyze(query)
        except ValueError as error:
            raise ToolError(str(error)) from error

    if matou_reader is not None:
        functional = FunctionEnvironmentService(reader, matou_reader, research)

        @server.tool(
            title="目标功能与环境关联",
            annotations=READ_ONLY,
            description=(
                "将一个目标Pfam的MetaT（或MetaG）按经核验且带来源版本的显式映射连接context_stat；"
                "默认温度及目标基因原数值和provided_sum，relative须明确选择。"
                "每个PANGAEA样本一行、合并映射到同一样本的重复记录，"
                "计算双侧置换Spearman及散点数据。sample_names省略即全范围；"
                "缺失或未映射不补零，相对信号零分母为空。无映射时返回blocked，不按名称推断。"
            ),
        )
        def function_environment(query: FunctionEnvironmentQuery) -> FunctionEnvironmentResult:
            try:
                return functional.analyze(query)
            except ValueError as error:
                raise ToolError(str(error)) from error
