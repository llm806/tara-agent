"""验证已有缓存的序列升级不会改旧目录或发布不完整来源。"""

import hashlib
import runpy
from pathlib import Path

import httpx
import pytest

from tara_agent.analysis.function_atlas import MatouAtlasService
from tara_agent.analysis.function_atlas_models import AtlasQuery, SequenceQuery
from tara_agent.analysis.function_study_prepare import prepare_study
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway

from .test_data_service import TOKEN
from .test_data_service import service_settings as service_settings
from .test_matou_function_data import dataset as dataset
from .test_matou_function_data import prepare

UPGRADE = runpy.run_path(str(Path(__file__).parents[1] / "deploy/prepare_matou_sequences.py"))


@pytest.fixture
def ready_study(dataset, tmp_path):
    prepare(dataset)
    directory = dataset[3]
    prepare_study(directory, min_free_gib=0)
    raw = tmp_path / "fasta-source"
    raw.mkdir()
    fasta = raw / "MATOU-v1.5.fna"
    fasta.write_text(">MATOU-v1.5.1\nACGTNN\n>MATOU-v1.5.2\nGGTT\n>MATOU-v1.5.3\nAAAA\n")
    return directory, fasta, tmp_path / "upgraded"


def upgrade(directory, fasta, output):
    return UPGRADE["prepare_sequences"](
        directory,
        fasta,
        output,
        pfams=["PF00002"],
        min_free_gib=0,
        expected_fasta_md5=hashlib.md5(fasta.read_bytes(), usedforsecurity=False).hexdigest(),
    )


def test_reuse_cache_preserve_source_and_query_sequences(ready_study):
    directory, fasta, output = ready_study
    before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    result_before = MatouAtlasService(MatouDataReader(directory)).atlas(AtlasQuery(assay="MetaG"))
    report = upgrade(directory, fasta, output)
    assert report["functional_cache_reused"] and report["sequence_count"] == 2
    service = MatouAtlasService(MatouDataReader(output))
    result_after = service.atlas(AtlasQuery(assay="MetaG"))
    assert result_before.ranks == result_after.ranks
    assert result_before.observations == result_after.observations
    sequence = service.sequences(SequenceQuery(pfam_accession="PF00002", limit=1, offset=1))
    assert sequence.total == 2 and sequence.sequences[0].geneID == 2
    assert sequence.sequences[0].sequence == "GGTT"
    assert before == {
        p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }
    for sample in service.reader.manifest.samples:
        assert (output / sample.artifact.path).stat().st_ino != (
            directory / sample.artifact.path
        ).stat().st_ino


def test_bad_md5_and_incomplete_fasta_do_not_publish(ready_study):
    directory, fasta, output = ready_study
    with pytest.raises(ValueError, match="MD5"):
        UPGRADE["prepare_sequences"](directory, fasta, output, pfams=["PF00002"], min_free_gib=0)
    assert not output.exists()
    fasta.write_text(">MATOU-v1.5.1\nACGT\n")
    with pytest.raises(ValueError, match="缺少"):
        upgrade(directory, fasta, output)
    assert not (output / "manifest.json").exists()


def test_skip_publisher_md5_records_no_verification(ready_study, monkeypatch):
    directory, fasta, output = ready_study
    fasta.write_text(">MATOU-v1.5.1\nacgtNN\n>MATOU-v1.5.2\nGgtt\n>MATOU-v1.5.3\naaaa\n")

    def forbidden_md5(*args, **kwargs):
        raise AssertionError("选择跳过时不应调用 MD5")

    monkeypatch.setattr(hashlib, "md5", forbidden_md5)
    report = UPGRADE["prepare_sequences"](
        directory, fasta, output, pfams=["PF00002"], min_free_gib=0, expected_fasta_md5=None
    )
    assert report["publisher_md5_verification"] == "not_performed"
    assert report["publisher_md5_skip_reason"] == "operator_request"
    assert report["fasta_md5"] is None and report["md5_reference"] is None
    assert report["sequence_case"] == "normalized_to_uppercase"
    assert report["soft_mask_case_preserved"] is False
    assert len(report["fasta_sha256"]) == 64
    assert (
        MatouAtlasService(MatouDataReader(output))
        .sequences(SequenceQuery(pfam_accession="PF00002"))
        .total
        == 2
    )
    reader = MatouDataReader(output)
    result = MatouAtlasService(reader).sequences(SequenceQuery(gene_ids=[1, 2]))
    assert [item.sequence for item in result.sequences] == ["ACGTNN", "GGTT"]
    selection = result.metadata.provenance.filters["sequence_evidence"]["selection"]
    assert selection["sequence_case"] == "normalized_to_uppercase"
    assert selection["soft_mask_case_preserved"] is False


@pytest.mark.parametrize(
    "content, message",
    [
        (">MATOU-v1_1\nACGT\n", "标题"),
        (">MATOU-v1.5.1\nPROTEIN\n", "核酸"),
        (">MATOU-v1.5.1\nACGT\n", "缺少"),
    ],
)
def test_skip_publisher_md5_still_rejects_wrong_sequences(ready_study, content, message):
    directory, fasta, output = ready_study
    fasta.write_text(content)
    with pytest.raises(ValueError, match=message):
        UPGRADE["prepare_sequences"](
            directory, fasta, output, pfams=["PF00002"], min_free_gib=0, expected_fasta_md5=None
        )
    assert not output.exists()


def test_reject_existing_or_source_output(ready_study):
    directory, fasta, output = ready_study
    for path in [directory, directory / "child", fasta.parent / "child"]:
        with pytest.raises(ValueError, match="新目录"):
            upgrade(directory, fasta, path)
    output.mkdir()
    keep = output / "keep.txt"
    keep.write_text("existing")
    with pytest.raises(ValueError, match="新目录"):
        upgrade(directory, fasta, output)
    assert keep.read_text() == "existing"


def test_corrupt_cache_copy_does_not_publish(ready_study):
    directory, fasta, output = ready_study
    reader = MatouDataReader(directory)
    path = directory / reader.manifest.samples[0].artifact.path
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    with pytest.raises(ValueError, match="SHA-256"):
        upgrade(directory, fasta, output)
    assert not (output / "manifest.json").exists()


@pytest.mark.anyio
async def test_upgraded_sequences_through_remote_gateway(ready_study, service_settings, tmp_path):
    directory, fasta, output = ready_study
    report = upgrade(directory, fasta, output)
    settings = service_settings.model_copy(update={"matou_data_dir": output})
    app = create_app(settings, token=TOKEN)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    local = settings.model_copy(
        update={"data_service_url": "http://127.0.0.1:8011", "data_service_token_file": token}
    )
    gateway = RemoteToolGateway(local, transport=httpx.ASGITransport(app=app))
    verify = runpy.run_path(str(Path(__file__).parents[1] / "deploy/check_remote_sequences.py"))[
        "verify"
    ]
    verified = await verify(gateway, pfams=["PF00002"])
    assert verified["status"] == "passed" and verified["checks"][0]["total"] == 2
    assert verified["study_manifest_sha256"] == report["new_study_manifest_sha256"]
    assert verified["fasta_sha256"] == report["fasta_sha256"]
    with pytest.raises(ValueError, match="没有真实序列"):
        await verify(gateway, pfams=["PF99999"])
