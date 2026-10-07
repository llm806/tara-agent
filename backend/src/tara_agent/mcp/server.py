"""MCP 服务工厂和标准输入输出入口。"""

from mcp.server import MCPServer

from tara_agent.analysis import TaraComputeService, TaraQueryService
from tara_agent.analysis.function_service import MatouFunctionService
from tara_agent.config import get_settings
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data.research_context import ResearchContext
from tara_agent.mcp.function_tools import register_function_tools
from tara_agent.mcp.research_tools import register_research_tools
from tara_agent.mcp.tools import register_tools


def create_server(
    reader: ProcessedDataReader | None = None,
    *,
    matou_reader: MatouDataReader | None = None,
    research: ResearchContext | None = None,
) -> MCPServer:
    """基于一个已校验的处理后数据读取器创建 Tara MCP 服务。"""

    if reader is None:
        settings = get_settings()
        reader = ProcessedDataReader(settings.processed_data_dir)
        if settings.matou_data_dir is not None:
            matou_reader = MatouDataReader(settings.matou_data_dir)

    server = MCPServer(
        name="tara-agent",
        title="Tara Agent",
        version="0.1.0",
        instructions=(
            "Query and analyze validated Tara Oceans data using available tools. "
            "V4 and V9 require an explicit marker and remain independent. "
            "MATOU requires an explicit MetaG/MetaT assay and original sample name; "
            "candidate function signals do not establish physical units or DNA/RNA pairing."
        ),
    )
    register_tools(
        server,
        query_service=TaraQueryService(reader),
        compute_service=TaraComputeService(reader),
    )
    if matou_reader is not None:
        register_function_tools(server, MatouFunctionService(matou_reader))
    register_research_tools(
        server, reader, matou_reader, research or ResearchContext(reader.processed_dir)
    )
    return server


def main() -> None:
    """通过标准输入输出传输方式运行 Tara MCP 服务。"""

    create_server().run(transport="stdio")
