"""使用虚构敏感文件和确定性依赖，验证授权闭环而非真实密钥读取。"""
from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import Response

from sirchmunk.agentic.tools import FileReadTool, KnowledgeQueryTool, TreeNavigationTool
from sirchmunk.doc_qa import DocFile, _extract_text, collect_doc_files
from sirchmunk.retrieve.text_retriever import GrepRetriever
from sirchmunk.scan.dir_scanner import DirectoryScanner
from sirchmunk.schema.search_context import SearchContext
from sirchmunk.search import AgenticSearch, _PathScope, _TreeNavCache
from sirchmunk.storage.knowledge_storage import KnowledgeStorage
from sirchmunk.utils.access_policy import (
    AccessDenied, AccessPolicy, bind_access, cache_partition, cached_sources_allowed,
    current_policy, open_source, require_source, resolve_search_paths, scoped_search, source_allowed,
)
from sirchmunk.utils.document_extractor import DocumentExtractor
from sirchmunk.utils.file_utils import get_fast_hash
from sirchmunk_mcp.config import Config, LLMConfig, MCPServerConfig, SirchmunkConfig
from sirchmunk_mcp.server import MCPHttpSecurity, create_server, run_http_server
from sirchmunk_mcp.service import SirchmunkService


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    docs.mkdir()
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    (work / "uploads").mkdir()
    private = tmp_path / "private"
    private.mkdir()
    document = docs / "说明.txt"
    document.write_text("公开资料 public finding 42", encoding="utf-8")
    secret = private / "sentinel.txt"
    secret.write_text("PRIVATE_SENTINEL_NOT_A_REAL_SECRET", encoding="utf-8")
    monkeypatch.setenv("SIRCHMUNK_WORK_PATH", str(work))
    monkeypatch.setenv("SIRCHMUNK_ALLOWED_PATHS", str(docs))
    monkeypatch.setenv("SIRCHMUNK_SEARCH_PATHS", str(docs))
    monkeypatch.setenv("SIRCHMUNK_ENABLE_CLUSTER_REUSE", "false")
    return SimpleNamespace(docs=docs, work=work, document=document, secret=secret,
                           policy=AccessPolicy.from_paths([docs]))


def config_for(corpus, transport="http", token="fixture-token"):
    return Config(
        llm=LLMConfig(api_key="unused-fixture-key"),
        sirchmunk=SirchmunkConfig(work_path=corpus.work, paths=[str(corpus.docs)],
                                enable_cluster_reuse=False),
        mcp=MCPServerConfig(transport=transport, api_token=SecretStr(token) if token else None),
    )


def service_for(corpus, *, transport="http", token="fixture-token"):
    service = object.__new__(SirchmunkService)
    service.config = config_for(corpus, transport, token)
    service.initialized = True
    service.searcher = SimpleNamespace(search=AsyncMock(return_value="public result"))
    return service


def test_path_boundaries_and_symlinks(corpus):
    sibling = corpus.docs.parent / "documents-backup"
    sibling.mkdir()
    safe_link = corpus.docs / "safe.txt"
    safe_link.symlink_to(corpus.document)
    unsafe_link = corpus.docs / "escape.txt"
    unsafe_link.symlink_to(corpus.secret)
    escaped_dir = corpus.docs / "outside"
    escaped_dir.symlink_to(corpus.secret.parent, target_is_directory=True)
    with bind_access(corpus.policy):
        assert require_source(safe_link) == str(corpus.document)
        assert not source_allowed(unsafe_link)
        assert not source_allowed(escaped_dir / "sentinel.txt")
        assert not source_allowed(corpus.docs / ".." / "private" / "sentinel.txt")
        assert not source_allowed(sibling)
        assert not source_allowed("https://localhost/private")
        assert not source_allowed("-rga-option")
        assert not _PathScope().contains(str(corpus.secret))
        with pytest.raises(AccessDenied):
            require_source(corpus.secret)
    assert source_allowed(corpus.secret)


def test_exact_file_scope_and_nested_policy_cannot_widen(corpus):
    with pytest.raises(AccessDenied):
        AccessPolicy(())
    with pytest.raises(AccessDenied):
        AccessPolicy(((),))
    with bind_access(corpus.policy):
        with bind_access(AccessPolicy.from_paths([corpus.document])):
            assert source_allowed(corpus.document)
            assert not source_allowed(corpus.docs)
            with bind_access(AccessPolicy.from_paths([corpus.secret.parent])):
                assert not source_allowed(corpus.secret)
            with bind_access(None):
                assert not source_allowed(corpus.secret)
        assert source_allowed(corpus.docs)
    assert current_policy() is None


@pytest.mark.parametrize("blank", [None, [], "", ["", "  "]])
def test_default_paths_are_authorized_without_cwd(corpus, monkeypatch, blank):
    assert resolve_search_paths(blank, strict=True) == [str(corpus.docs)]
    monkeypatch.delenv("SIRCHMUNK_SEARCH_PATHS")
    assert resolve_search_paths(blank, strict=True) == [str(corpus.work / "data"), str(corpus.work / "uploads")]
    with pytest.raises(AccessDenied):
        resolve_search_paths([str(corpus.document), str(corpus.secret)], strict=True)
    monkeypatch.setenv("SIRCHMUNK_SEARCH_PATHS", str(corpus.secret.parent))
    with pytest.raises(AccessDenied):
        resolve_search_paths(blank, strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["FAST", "DEEP", "FILENAME_ONLY"])
@pytest.mark.parametrize("format", ["rich", "minimal", "context", "json"])
async def test_mcp_rejects_entire_mixed_request_before_search(corpus, mode, format):
    service = service_for(corpus)
    with pytest.raises(AccessDenied):
        await service.search("public", paths=[str(corpus.document), str(corpus.secret)],
                             mode=mode, response_format=format)
    service.searcher.search.assert_not_awaited()
    assert current_policy() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["FAST", "DEEP", "FILENAME_ONLY"])
@pytest.mark.parametrize("format", ["rich", "minimal", "context", "json"])
async def test_mcp_legitimate_request_binds_policy(corpus, mode, format):
    service = service_for(corpus)

    async def search(**kwargs):
        assert kwargs["paths"] == [str(corpus.docs)]
        assert source_allowed(corpus.document)
        assert not source_allowed(corpus.secret)
        return "public result"

    service.searcher.search.side_effect = search
    assert await service.search("public", mode=mode, response_format=format) == "public result"
    assert current_policy() is None


@pytest.mark.asyncio
async def test_stdio_compatibility_and_explicit_whitelist(corpus, monkeypatch):
    service = service_for(corpus, transport="stdio", token="")
    with pytest.raises(AccessDenied):
        await service.search("public", paths=[str(corpus.secret)])
    monkeypatch.delenv("SIRCHMUNK_ALLOWED_PATHS")
    await service.search("public", paths=[str(corpus.secret)])
    service.searcher.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_requires_token_before_service_initialization(corpus, monkeypatch):
    initialize = Mock()
    monkeypatch.setattr("sirchmunk_mcp.server.create_server", initialize)
    for host in ("localhost", "0.0.0.0", "::1"):
        config = config_for(corpus, token="")
        config.mcp.host = host
        with pytest.raises(ValueError, match="SIRCHMUNK_API_TOKEN"):
            await run_http_server(config)
    initialize.assert_not_called()
    monkeypatch.setenv("LLM_API_KEY", "unused-fixture-key")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.delenv("SIRCHMUNK_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SIRCHMUNK_API_TOKEN"):
        Config.from_env()
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    assert Config.from_env().mcp.transport == "stdio"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/sse", "/messages/?session_id=stolen-session"])
@pytest.mark.parametrize("auth", [None, "Bearer incorrect", "Basic fixture-token", "Bearer fixture-token"])
async def test_http_auth_on_handshake_and_each_message(corpus, path, auth):
    called = Mock()

    async def downstream(scope, receive, send):
        called()
        await Response(status_code=202 if scope["method"] == "POST" else 200)(scope, receive, send)

    app = MCPHttpSecurity(downstream, config_for(corpus))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        headers = {"Authorization": auth} if auth else {}
        response = await client.request("POST" if path.startswith("/messages") else "GET", path, headers=headers)
    if auth == "Bearer fixture-token":
        assert response.status_code in (200, 202)
        called.assert_called_once()
    else:
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "fixture-token" not in response.text
        called.assert_not_called()


@pytest.mark.asyncio
async def test_http_host_origin_and_duplicate_authorization(corpus):
    downstream = AsyncMock()
    app = MCPHttpSecurity(downstream, config_for(corpus))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        for headers in (
            [("Authorization", "Bearer fixture-token"), ("Host", "attacker.invalid")],
            [("Authorization", "Bearer fixture-token"), ("Origin", "http://attacker.invalid")],
            [("Authorization", "Bearer fixture-token"), ("Authorization", "Bearer fixture-token")],
        ):
            response = await client.get("/sse", headers=headers)
            assert response.status_code in (401, 403)
    downstream.assert_not_awaited()
    assert not MCPHttpSecurity._matches("localhost:8080.attacker", ["localhost:*"])
    assert MCPHttpSecurity._matches("[::1]:8080", ["[::1]:*"])


@pytest.mark.asyncio
async def test_model_file_read_and_tree_navigation_cannot_escape(corpus):
    context = SearchContext()
    navigate = AsyncMock(return_value="should not execute")
    with bind_access(corpus.policy):
        text, metadata = await FileReadTool().execute(context, file_paths=[str(corpus.document), str(corpus.secret)])
        assert metadata["denied"]
        assert "PRIVATE_SENTINEL" not in text
        assert not context.read_file_ids
        text, metadata = await TreeNavigationTool(navigate).execute(
            context, file_path=str(corpus.secret), query="retrieve",
        )
        assert metadata["denied"]
        navigate.assert_not_awaited()
        text, metadata = await FileReadTool().execute(context, file_paths=[str(corpus.document)])
        assert "公开资料" in text
        assert str(corpus.document) in metadata["files_read"]


def cluster_for(*sources):
    return SimpleNamespace(id="fixture-cluster", name="fixture", description="cached description",
                           content="cached answer", patterns=[], related_clusters=[],
                           search_results=list(sources),
                           evidences=[SimpleNamespace(file_or_url=s) for s in sources])


@pytest.mark.asyncio
async def test_mixed_and_unknown_cached_sources_rejected_whole(corpus):
    allowed = cluster_for(str(corpus.document))
    mixed = cluster_for(str(corpus.document), str(corpus.secret))
    mixed.content = "PRIVATE_SENTINEL_NOT_A_REAL_SECRET"
    unknown = cluster_for()
    storage = SimpleNamespace(find=AsyncMock(return_value=[allowed, mixed, unknown]))
    with bind_access(corpus.policy):
        assert cached_sources_allowed(allowed)
        assert not cached_sources_allowed(mixed)
        assert not cached_sources_allowed(unknown)
        text, _ = await KnowledgeQueryTool(storage).execute(SearchContext(), query="fixture")
        assert "cached answer" in text
        assert "PRIVATE_SENTINEL" not in text
        assert not cached_sources_allowed({"search_results": "not-json", "content": "secret"})
    assert cached_sources_allowed(mixed)


@pytest.mark.asyncio
async def test_storage_get_find_authorize_full_cluster(corpus):
    mixed = cluster_for(str(corpus.document), str(corpus.secret))
    allowed = cluster_for(str(corpus.document))
    storage = object.__new__(KnowledgeStorage)
    storage.table_name = "fixture_table"
    storage._check_and_reload = lambda: None
    storage._row_to_cluster = lambda row: row
    storage.db = SimpleNamespace(fetch_one=Mock(return_value=mixed), fetch_all=Mock(return_value=[mixed, allowed]))
    with bind_access(corpus.policy):
        assert await storage.get("fixture") is None
        assert await storage.find("fixture") == [allowed]
    storage.db = None


@pytest.mark.asyncio
async def test_direct_reads_hash_and_extractors_deny_before_io(corpus, monkeypatch):
    configure = Mock()
    monkeypatch.setattr("sirchmunk.utils.document_extractor.DocumentExtractor._build_config", configure)
    with bind_access(corpus.policy):
        with pytest.raises(AccessDenied):
            get_fast_hash(corpus.secret)
        with pytest.raises(AccessDenied):
            await DocumentExtractor.extract(corpus.secret)
        with pytest.raises(AccessDenied):
            await DocumentExtractor.extract_isolated(corpus.secret)
        with pytest.raises(AccessDenied):
            await DocumentExtractor.batch_extract([corpus.document, corpus.secret])
        with pytest.raises(AccessDenied):
            DocumentExtractor.extract_pages(corpus.secret, [1])
        with pytest.raises(AccessDenied):
            AgenticSearch._resolve_file_hints([str(corpus.secret.parent)], ["sentinel.txt"])
        with pytest.raises(AccessDenied):
            await AgenticSearch._fast_read_file_head(str(corpus.secret))
        with pytest.raises(AccessDenied):
            AgenticSearch._read_text_evidence(corpus.secret, "private", 100)
        with pytest.raises(AccessDenied):
            await _extract_text(DocFile(str(corpus.secret), 10, ".txt", True))
        assert get_fast_hash(corpus.document)
        assert "公开资料" in AgenticSearch._read_text_evidence(corpus.document, "public", 100)
    assert get_fast_hash(corpus.secret)
    configure.assert_not_called()


@pytest.mark.asyncio
async def test_directory_scan_symlink_escape_cache_and_worker_scope(corpus, monkeypatch):
    (corpus.docs / "escaped.txt").symlink_to(corpus.secret)
    (corpus.docs / "escaped-directory").symlink_to(corpus.secret.parent, target_is_directory=True)
    scanner = DirectoryScanner(cache_mode="memory", max_files=20)
    original = scanner._extract_single
    scopes = []

    def inspect_worker(path):
        scopes.append(current_policy())
        return original(path)

    monkeypatch.setattr(scanner, "_extract_single", inspect_worker)
    with bind_access(corpus.policy):
        first = await scanner.scan([corpus.docs])
        second = await scanner.scan([corpus.docs])
        assert len(first.candidates) == 1
        assert second.cache_hit
        assert all(policy is not None for policy in scopes)
        assert "PRIVATE_SENTINEL" not in json.dumps(first.candidates[0].to_dict())
        assert collect_doc_files([str(corpus.docs)])[0].path == str(corpus.document)
        with pytest.raises(AccessDenied):
            await scanner.scan([corpus.secret.parent])


@pytest.mark.asyncio
async def test_native_format_fallback_and_cache_revalidate_source(corpus):
    log = corpus.docs / "events.log"
    log.write_text("Record Exact-7788 has value 42", encoding="utf-8")
    (corpus.docs / "escape.log").symlink_to(corpus.secret)
    retriever = GrepRetriever(work_path=corpus.work)
    with bind_access(corpus.policy):
        first = await retriever.retrieve_native_formats(["Exact-7788"], path=[corpus.docs])
        second = await retriever.retrieve_native_formats(["Exact-7788"], path=[corpus.docs])
        assert len(first) == 1
        assert second[0]["matches"][0]["_search_cache_hit"]
        with pytest.raises(AccessDenied):
            await retriever.retrieve("private", path=[corpus.secret])
        with pytest.raises(AccessDenied):
            await retriever.list_files(path=str(corpus.secret.parent))
        with pytest.raises(AccessDenied):
            await retriever.retrieve_by_filename("sentinel", path=corpus.secret.parent)


@pytest.mark.asyncio
async def test_keyword_rg_rga_passes_keep_authorized_results(corpus):
    retriever = GrepRetriever(work_path=corpus.work)
    with bind_access(corpus.policy):
        matches = await retriever.retrieve("public", path=[corpus.docs], literal=True, regex=False)
        assert any(item.get("type") == "match" for item in matches)
        assert all(source_allowed(item["data"]["path"]["text"]) for item in matches)
        assert "PRIVATE_SENTINEL" not in json.dumps(matches)
    assert current_policy() is None


@pytest.mark.asyncio
async def test_concurrent_scope_reset_cancellation_and_thread_copy(corpus):
    async def worker(policy, should_allow):
        with bind_access(policy):
            await asyncio.sleep(0)
            assert source_allowed(corpus.document) == should_allow
            copied = contextvars.copy_context()
            result = await asyncio.get_running_loop().run_in_executor(
                None, copied.run, source_allowed, corpus.document,
            )
            assert result == should_allow
        assert current_policy() is None

    await asyncio.gather(worker(corpus.policy, True), worker(AccessPolicy.from_paths([corpus.secret]), False))
    ready = asyncio.Event()

    async def cancelled():
        with bind_access(corpus.policy):
            ready.set()
            await asyncio.sleep(100)

    task = asyncio.create_task(cancelled())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_policy() is None
    assert pickle.loads(pickle.dumps(corpus.policy)) == corpus.policy


@pytest.mark.asyncio
async def test_extraction_child_receives_explicit_policy(corpus, monkeypatch):
    captured = []

    def child(file_path, profile, policy):
        captured.append(policy)
        return {"content": "public child result"}

    monkeypatch.setattr("sirchmunk.utils.document_extractor._run_extraction_in_child", child)
    with bind_access(corpus.policy):
        result = await DocumentExtractor.extract_isolated(corpus.document)
    assert result.content == "public child result"
    assert captured == [corpus.policy]


@pytest.mark.asyncio
async def test_scoped_search_is_narrowed_without_changing_local_behavior(corpus):
    calls = []

    class Search:
        def _resolve_paths(self, paths):
            return paths

        @scoped_search
        async def search(self, paths):
            calls.append(source_allowed(corpus.secret))
            return require_source(paths[0])

    search = Search()
    with bind_access(corpus.policy):
        with pytest.raises(AccessDenied):
            await search.search([str(corpus.secret)])
        assert calls == []
        assert await search.search([str(corpus.document)]) == str(corpus.document)
    assert await search.search([str(corpus.secret)]) == str(corpus.secret)
    assert calls == [False, True]


def test_cache_partition_changes_and_local_keys_preserved(corpus):
    assert cache_partition("key") == "key"
    old = _TreeNavCache._key(str(corpus.document), "public")
    with bind_access(corpus.policy):
        scoped = _TreeNavCache._key(str(corpus.document), "public")
        assert scoped != old
        with bind_access(AccessPolicy.from_paths([corpus.document])):
            assert _TreeNavCache._key(str(corpus.document), "public") != scoped
    assert _TreeNavCache._key(str(corpus.document), "public") == old


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["FAST", "DEEP", "FILENAME_ONLY"])
@pytest.mark.parametrize("format", ["rich", "minimal", "context", "json"])
async def test_core_output_equivalence_for_authorized_documents(corpus, mode, format, record_property):
    import time
    core = object.__new__(AgenticSearch)
    core.paths = None
    core._logger = SimpleNamespace(info=AsyncMock(), success=AsyncMock(), warning=AsyncMock(),
                                   debug=AsyncMock(), error=AsyncMock())
    core.grep_retriever = GrepRetriever(work_path=corpus.work)
    core.knowledge_evolver = None
    core.llm = SimpleNamespace(bind_token_budget=lambda _: None,
                               token_budget_snapshot=lambda: {}, reset_token_budget=lambda _: None)

    async def deterministic_answer(**kwargs):
        context = SearchContext()
        text, _ = await FileReadTool().execute(context, file_paths=[str(corpus.document)])
        return text, AgenticSearch._make_answer_cluster(kwargs["query"], text,
                    file_paths=[str(corpus.document)]), context

    core._search_fast = AsyncMock(side_effect=deterministic_answer)
    core._search_deep = AsyncMock(side_effect=deterministic_answer)

    def normalized(result):
        if isinstance(result, SearchContext):
            result = result.to_dict()
        if isinstance(result, dict):
            return result["answer"], result["cluster"]["search_results"]
        return result

    kwargs = dict(query="说明", paths=[str(corpus.docs)], mode=mode, response_format=format)
    started = time.perf_counter()
    trusted = await core.search(**kwargs)
    trusted_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    with bind_access(corpus.policy):
        restricted = await core.search(**kwargs)
    restricted_ms = (time.perf_counter() - started) * 1000
    assert normalized(restricted) == normalized(trusted)
    assert "PRIVATE_SENTINEL" not in str(restricted)
    record_property("trusted_elapsed_ms", trusted_ms)
    record_property("restricted_elapsed_ms", restricted_ms)
    if mode == "FILENAME_ONLY":
        assert Path(restricted[0]["path"]) == corpus.document
    else:
        assert core._search_fast.await_count + core._search_deep.await_count == 2


@pytest.mark.asyncio
async def test_similarity_filter_is_authorization_not_half_coverage(corpus):
    allowed = cluster_for(str(corpus.document))
    mixed = cluster_for(str(corpus.document), str(corpus.secret))
    def row(identifier, source):
        return (identifier, "fixture", "description", 0.9, 0.5, json.dumps(source), 0.99)
    rows = [row("mixed", mixed.search_results), row("allowed", allowed.search_results)]
    storage = object.__new__(KnowledgeStorage)
    storage.table_name = "fixture_table"
    storage._check_and_reload = lambda: None
    storage._row_to_cluster = lambda value: value
    storage.db = SimpleNamespace(
        fetch_all=Mock(side_effect=[rows, []]),
        fetch_one=Mock(side_effect=lambda sql, params=None: [2] if "COUNT" in sql else
                       {"mixed": mixed, "allowed": allowed}[params[0]]),
    )
    assert storage._compute_path_coverage(mixed.search_results, [str(corpus.docs)]) == 0.5
    with bind_access(corpus.policy):
        result = await storage.search_similar_clusters([1.0] * 384, search_paths=[str(corpus.docs)])
    assert [item["id"] for item in result] == ["allowed"]
    storage.db = None


@pytest.mark.asyncio
async def test_compile_catalog_and_spec_provenance(corpus):
    core = object.__new__(AgenticSearch)
    core.work_path = corpus.work
    core._logger = SimpleNamespace(debug=AsyncMock())
    core.spec_path = corpus.work / ".cache" / "spec"
    core.spec_path.mkdir(parents=True)
    catalog = corpus.work / ".cache" / "compile" / "document_catalog.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps([
        {"path": str(corpus.document), "summary": "公开资料"},
        {"path": str(corpus.secret), "summary": "PRIVATE_SENTINEL_NOT_A_REAL_SECRET"},
    ]), encoding="utf-8")
    with bind_access(corpus.policy):
        assert core._load_document_catalog() == [{"path": str(corpus.document), "summary": "公开资料"}]
        spec = core._spec_file(str(corpus.docs))
        spec.write_text(json.dumps({"files_read": [str(corpus.document), str(corpus.secret)],
                                  "summary": "PRIVATE_SENTINEL_NOT_A_REAL_SECRET"}), encoding="utf-8")
        assert await core._load_spec_context([str(corpus.docs)]) == ""
        spec.write_text(json.dumps({"files_read": [str(corpus.document)], "summary": "公开资料",
                                  "cached_at": "2099-01-01T00:00:00+00:00"}), encoding="utf-8")
        assert "公开资料" in await core._load_spec_context([str(corpus.docs)])
        # resources 也是已知来源，不能仅授权 evidences。
        assert not cached_sources_allowed({"search_results": [str(corpus.document)],
                   "resources": [{"type": "file", "value": str(corpus.secret)}]})


@pytest.mark.asyncio
async def test_web_search_rejects_paths_and_auth_returns_401(corpus, monkeypatch):
    # 初始化副作用仅发生在测试专用工作目录，不读取用户运行环境。
    api = importlib.import_module("sirchmunk.api.search")
    chat = importlib.import_module("sirchmunk.api.chat")
    with pytest.raises(Exception) as exc:
        api._build_search_kwargs(api.SearchRequest(query="public", paths=[str(corpus.secret)]))
    assert exc.value.status_code == 403
    with pytest.raises(AccessDenied):
        chat._resolve_rag_paths(str(corpus.secret))
    assert chat._resolve_rag_paths(str(corpus.document))[0] == [str(corpus.document)]
    searcher = SimpleNamespace(search=AsyncMock())
    factory = Mock(return_value=searcher)
    monkeypatch.setattr(api, "_get_search_instance", factory)
    with pytest.raises(Exception) as exc:
        await api.execute_search(api.SearchRequest(query="public", paths=[str(corpus.secret)]))
    assert exc.value.status_code == 403
    factory.assert_not_called()
    searcher.search.assert_not_awaited()
    with pytest.raises(Exception) as exc:
        await api.execute_search_stream(api.SearchRequest(query="public", paths=[str(corpus.secret)]), Mock())
    assert exc.value.status_code == 403
    main = importlib.import_module("sirchmunk.api.main")
    monkeypatch.setenv("SIRCHMUNK_API_TOKEN", "fixture-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        response = await client.get("/api/v1/file-browser")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    # 在 pytest 捕获流关闭前结束测试用数据库的后台同步。
    knowledge = importlib.import_module("sirchmunk.api.knowledge")
    storage = getattr(knowledge, "km", None)
    if storage is not None:
        import atexit
        storage.close()
        atexit.unregister(storage._shutdown_parquet_sync)
        storage.db = None


@pytest.mark.asyncio
async def test_pdf_docx_extensionless_and_safe_symlink(corpus, monkeypatch):
    from docx import Document
    from pypdf import PdfWriter

    raw = corpus.docs / "raw-shard"
    raw.write_text("原始无扩展名语料 public raw evidence", encoding="utf-8")
    linked = corpus.docs / "raw-alias"
    linked.symlink_to(raw)
    office = corpus.docs / "document.docx"
    document = Document()
    document.add_paragraph("公开 Office evidence")
    document.save(office)
    pdf = corpus.docs / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    with bind_access(corpus.policy):
        text, _ = await FileReadTool().execute(SearchContext(), file_paths=[str(linked)])
        assert "无扩展名" in text
        output = await DocumentExtractor.extract(office)
        assert "Office evidence" in output.content
        parent_extract = AsyncMock(wraps=DocumentExtractor.extract)
        monkeypatch.setattr(DocumentExtractor, "extract", parent_extract)
        monkeypatch.setattr("sirchmunk.utils.document_extractor._EXTRACT_TIMEOUT_S", 20)
        isolated = await DocumentExtractor.extract_isolated(office)
        assert "Office evidence" in isolated.content
        parent_extract.assert_not_awaited()  # 确认真正经过子进程，不是回退掩盖失败。
        assert len(DocumentExtractor.extract_pages(pdf, [1])) == 1
    assert linked.is_symlink()


def test_posix_reader_blocks_symlink_swap_after_check(corpus, monkeypatch):
    import os
    if os.name != "posix":
        pytest.skip("目录描述符防护为 POSIX 专用；其他平台由部署隔离约束")
    original_open = os.open
    switched = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal switched
        if path == corpus.document.name and not switched:
            # 只替换测试专用文件；保留原文档供 fixture 自身清理。
            corpus.document.rename(corpus.docs / "original.txt")
            corpus.document.symlink_to(corpus.secret)
            switched = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    with bind_access(corpus.policy):
        with pytest.raises(AccessDenied):
            with open_source(corpus.document) as stream:
                stream.read()
    assert switched


@pytest.mark.asyncio
async def test_jsonlines_read_scope(corpus):
    shard = corpus.docs / "raw"
    shard.write_text(json.dumps({"id": "new-entity", "text": ["公开 public 原文"]}), encoding="utf-8")
    search = object.__new__(AgenticSearch)
    search._jsonlines_cache = {}
    with bind_access(corpus.policy):
        assert search._is_jsonlines_corpus(str(shard))
        assert "原文" in search._extract_articles_from_jsonlines(str(shard), ["new-entity"], "public", 500)
        with pytest.raises(AccessDenied):
            search._is_jsonlines_corpus(str(corpus.secret))
        with pytest.raises(AccessDenied):
            search._extract_articles_from_jsonlines(str(corpus.secret), [], "private", 500)


@pytest.mark.asyncio
async def test_legacy_sse_rpc_replays_attack_chain_in_process(corpus, monkeypatch):
    import re

    service = service_for(corpus)
    core = object.__new__(AgenticSearch)
    core.paths = None
    core._logger = SimpleNamespace(info=AsyncMock(), success=AsyncMock(), warning=AsyncMock(),
                                   debug=AsyncMock(), error=AsyncMock())
    core.grep_retriever = GrepRetriever(work_path=corpus.work)
    core.knowledge_evolver = None
    core.llm = SimpleNamespace(bind_token_budget=lambda _: None,
                               token_budget_snapshot=lambda: {}, reset_token_budget=lambda _: None)

    async def deterministic_answer(**kwargs):
        context = SearchContext()
        text, _ = await FileReadTool().execute(context, file_paths=[str(corpus.document)])
        cluster = AgenticSearch._make_answer_cluster(kwargs["query"], text, file_paths=[str(corpus.document)])
        return text, cluster, context

    core._search_fast = AsyncMock(side_effect=deterministic_answer)
    core._search_deep = AsyncMock(side_effect=deterministic_answer)
    service.searcher = core
    monkeypatch.setattr("sirchmunk_mcp.server.SirchmunkService", lambda _: service)
    mcp = create_server(config_for(corpus))
    app = MCPHttpSecurity(mcp.sse_app(), config_for(corpus))
    chunks = []
    disconnect = asyncio.Event()
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(event):
        if event["type"] == "http.response.body":
            chunks.append(event.get("body", b"").decode("utf-8"))

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/sse",
             "raw_path": b"/sse", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"localhost"), (b"authorization", b"Bearer fixture-token")],
             "client": ("127.0.0.1", 12345), "server": ("localhost", 80)}
    stream = asyncio.create_task(app(scope, receive, send))

    async def wait_for(predicate):
        while not predicate():
            if stream.done():
                await stream
            await asyncio.sleep(0.01)

    def reply(identifier):
        for chunk in chunks:
            for line in chunk.splitlines():
                if line.startswith("data: {"):
                    payload = json.loads(line[6:])
                    if payload.get("id") == identifier:
                        return payload
        return None

    try:
        await asyncio.wait_for(wait_for(lambda: any("event: endpoint" in c for c in chunks)), 5)
        endpoint = re.search(r"data: (/messages/[^\r\n]+)", "".join(chunks)).group(1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.post(endpoint, json={"jsonrpc": "2.0", "method": "initialize", "id": 1})).status_code == 401
            headers = {"Authorization": "Bearer fixture-token"}
            initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}}
            assert (await client.post(endpoint, headers=headers, json=initialize)).status_code == 202
            await asyncio.wait_for(wait_for(lambda: reply(1) is not None), 5)
            await client.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            # 重放白名单外目录枚举、源码/配置原文读取的同类攻击；目标全部为虚构文件。
            for identifier, mode in enumerate(("FILENAME_ONLY", "FAST", "DEEP"), 2):
                call = {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
                    "name": "sirchmunk_search", "arguments": {"query": "sentinel", "mode": mode,
                    "paths": [str(corpus.secret if mode != "FILENAME_ONLY" else corpus.secret.parent)], "response_format": "context"}}}
                assert (await client.post(endpoint, headers=headers, json=call)).status_code == 202
                await asyncio.wait_for(wait_for(lambda: reply(identifier) is not None), 5)
                result = reply(identifier)["result"]
                assert result["isError"]
                assert "PRIVATE_SENTINEL" not in json.dumps(result)
            core._search_fast.assert_not_awaited()
            core._search_deep.assert_not_awaited()
            allowed_call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "sirchmunk_search", "arguments": {"query": "public", "mode": "FAST",
                "paths": [str(corpus.docs)], "response_format": "context"}}}
            await client.post(endpoint, headers=headers, json=allowed_call)
            await asyncio.wait_for(wait_for(lambda: reply(5) is not None), 5)
            assert not reply(5)["result"]["isError"]
            assert "公开资料" in reply(5)["result"]["content"][0]["text"]
    finally:
        disconnect.set()
        stream.cancel()
        try:
            await stream
        except asyncio.CancelledError:
            pass
    assert current_policy() is None
