"""推荐必须绑定真实工具和数据；复杂问题只在编排能力存在时展示。"""

import random

import httpx
import pytest

from tara_agent.agent.home_suggestions import HomeSuggestionService, describe_core_question
from tara_agent.agent.models import ToolName
from tara_agent.api.app import create_app
from tara_agent.api.schemas import QuestionSuggestionResponse
from tara_agent.data_service.client import RemoteToolGateway

from .test_data_service import service_settings as service_settings
from .test_matou_function_data import dataset as dataset
from .test_matou_remote_tools import matou_setup as matou_setup
from .test_multistep_agent import SequenceModel


@pytest.mark.anyio
async def test_remote_recommendations_are_balanced_and_bound_to_real_samples(matou_setup):
    _, local, service = matou_setup
    gateway = RemoteToolGateway(local, transport=httpx.ASGITransport(app=service))
    home = HomeSuggestionService(
        gateway, gateway, multi_step_available=True, random_source=random.Random(5)
    )
    batch = await home.get_batch(limit=4)
    assert {item.task_type for item in batch} == {
        "data_query",
        "statistical_analysis",
        "function_analysis",
        "multi_step_analysis",
    }
    function = await home._function_questions()
    assert any("MetaG 样本 G-A" in item.question for item in function)
    assert any("MetaT 样本 T-A" in item.question for item in function)
    assert all("Bacillariophyta" not in item.question for item in function)
    assert all("G-empty" not in item.question for item in function)
    assert all("matou_pfam" in item.datasets for item in function)
    sensitivity = [item for item in function if "sensitivity" in item.id]
    assert len(sensitivity) == 2
    assert all("同一样本" in item.limitation for item in sensitivity)
    assert all("min_iEvalue ≤ 1e-5" in item.question for item in sensitivity)
    assert not any("independent" in item.id for item in function)
    refreshed = await home.get_batch(limit=4, exclude_ids={i.id for i in batch})
    assert {i.id for i in refreshed}.isdisjoint(i.id for i in batch)
    legacy = HomeSuggestionService(gateway, gateway)
    assert all(i.task_type != "multi_step_analysis" for i in await legacy.get_batch(limit=8))


def test_complex_questions_require_supported_tools_and_marker_evidence():
    home = HomeSuggestionService(None, None, multi_step_available=True)
    core = [
        describe_core_question(
            QuestionSuggestionResponse(
                id=f"abundance-{marker}-0",
                category="丰度分布",
                question=f"{marker.upper()} 中 TestTaxon 在各样本的相对丰度如何？",
            )
        )
        for marker in ("v4", "v9")
    ]
    tools = {ToolName.TAXON_ABUNDANCE, ToolName.GET_SAMPLE_INFO, ToolName.DIVERSITY_ANALYSIS}
    questions = home._core_multistep_questions(core, tools)
    assert len(questions) == 5
    context = [q for q in questions if q.category == "丰度与环境核查"]
    assert all("全部符合条件" in q.question and "占比最高的属" in q.question for q in context)
    assert all("context_stat" in q.datasets for q in context)
    comparison = next(q for q in questions if q.id == "multi-marker-diversity")
    assert {"18s_v4", "18s_v9"} <= set(comparison.datasets)
    assert "独立计算" in comparison.limitation
    assert not home._core_multistep_questions(core, set())
    assert not any(
        q.id == "multi-marker-diversity" for q in home._core_multistep_questions(core[:1], tools)
    )


@pytest.mark.anyio
async def test_function_questions_do_not_require_unrelated_sequence_or_study_tools(matou_setup):
    _, local, service = matou_setup
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=service))

    class ProfileGateway:
        async def list_tools(self):
            return [
                t
                for t in await remote.list_tools()
                if t.name
                in {
                    ToolName.FIND_FUNCTION_SAMPLES,
                    ToolName.FUNCTION_PROFILE,
                    ToolName.FUNCTION_ATLAS,
                }
            ]

        async def call(self, name, arguments):
            assert name is ToolName.FIND_FUNCTION_SAMPLES
            return await remote.call(name, arguments)

    gateway = ProfileGateway()
    home = HomeSuggestionService(remote, gateway, random_source=random.Random(1))
    questions = await home.get_batch(limit=30)
    assert any(q.id == "function-MetaT-T-A" for q in questions)
    overall = next(q for q in questions if q.id == "function-overall-MetaT")
    assert "前 100 项" in overall.question
    assert "Example taxon" in overall.question
    assert "不等同于论文" in overall.limitation
    assert all("序列" not in q.question for q in questions)


@pytest.mark.anyio
async def test_large_overall_recommendation_requires_prepared_cache(matou_setup):
    _, local, service = matou_setup
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=service))

    class LargeCohortGateway:
        async def call(self, name, arguments):
            listing = await remote.call(name, arguments)
            return {**listing, "total": 1000}

    tools = {ToolName.FIND_FUNCTION_SAMPLES, ToolName.FUNCTION_PROFILE, ToolName.FUNCTION_ATLAS}
    home = HomeSuggestionService(None, LargeCohortGateway())
    assert not any(
        q.id.startswith("function-overall-") for q in await home._function_questions(tools)
    )
    home.function_cache_ready = lambda: True
    assert any(q.id == "function-overall-MetaT" for q in await home._function_questions(tools))


def test_old_remote_catalog_has_explicit_dataset_labels():
    # 旧科学服务仍返回三字段；本地根据已有受控编号契约补充显示信息。
    item = QuestionSuggestionResponse(
        id="association-v9-0-temperature", category="环境关联", question="现有目录问题"
    )
    described = describe_core_question(item)
    assert described.task_type == "statistical_analysis"
    assert set(described.datasets) == {"18s_v9", "context_general", "context_stat"}
    assert described.availability == "available"


@pytest.mark.anyio
async def test_product_api_exposes_real_multistep_questions_without_local_data(matou_setup):
    _, local, service = matou_setup
    app = create_app(
        local, agent_model=SequenceModel(), data_service_transport=httpx.ASGITransport(app=service)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/question-suggestions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(i["task_type"] == "multi_step_analysis" for i in items)
    assert all(i["datasets"] and i["availability"] == "available" for i in items)
    assert not local.processed_data_dir.exists()


@pytest.mark.anyio
async def test_no_matou_tools_produces_no_matou_recommendations(matou_setup):
    _, local, service = matou_setup
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=service))

    class CoreOnlyGateway:
        async def list_tools(self):
            return [
                t
                for t in await remote.list_tools()
                if t.name.value not in {"function_profile", "find_function_samples"}
            ]

        async def call(self, *args):
            raise AssertionError("未注册的 MATOU 工具不应被调用")

    home = HomeSuggestionService(remote, CoreOnlyGateway(), multi_step_available=True)
    batch = await home.get_batch(limit=8)
    assert any(i.task_type == "multi_step_analysis" for i in batch)
    assert all(not any(d.startswith("matou_") for d in i.datasets) for i in batch)
