from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts import generate_test_docs as docs


def test_gc_docs_cover_current_cartesian_cells_and_complete_evidence():
    cases = docs._image_case_sets("banana")["gemini_generate_content"]
    expected_counts = {"gemini-2.5-flash-image": 20, "gemini-3.1-flash-lite-image": 19,
                       "gemini-3.1-flash-image": 58, "gemini-3-pro-image": 45}
    assert len(cases) == 142
    assert all(case.expected_outcome in {"success", "rejection"} for case in cases)
    rendered = "\n".join(docs._render_banana_generate_content_evidence(cases))
    for model, count in expected_counts.items():
        rows = [case for case in cases if case.model_override == model]
        assert len(rows) == count
        observed = sum(case.expected_outcome == "observation" for case in rows)
        assert f"| `{model}` | {count} | {count - observed} | {observed} |" in rendered
    assert "384×3072" in rendered and "352×2928" in rendered
    assert "792×168" in rendered and "784×336" in rendered
    assert "模型门禁开启不等于所有单元已认证" in rendered


def test_gc_docs_preserve_omitted_image_size_and_unresolved_purpose():
    cases = docs._image_case_sets("banana")["gemini_generate_content"]
    baseline = next(case for case in cases if case.model_override == "gemini-2.5-flash-image")
    settings = docs._image_case_settings(baseline, {"gemini_generate_content"})
    assert "imageSize" not in settings["generationConfig"]["imageConfig"]
    assert settings["api_version"] == "v1beta"
    unresolved = replace(
        baseline, name="synthetic_unpublished_gc_case", expected_outcome="observation",
        expected_size=None,
        metadata={"banana_gc_exact": True, "banana_gc_candidate": True,
                  "matrix_group": "documented"},
    )
    assert "缺少该单元的已发布 v1beta 证据" in docs._banana_generate_content_purpose(unresolved)
    assert "不计入认证" in docs._banana_generate_content_purpose(unresolved)
    rendered = "\n".join(docs._render_banana_generate_content_evidence([unresolved]))
    assert "| `gemini-2.5-flash-image` | 1 | 0 | 1 |" in rendered


def test_history_documentation_reads_pinned_policy_evidence(monkeypatch):
    history = "\n".join(docs._render_banana_generate_content_history())
    assert "历史 v1 证据（只读）" in history
    assert "gemini_3_1_flash_lite_image_generate_content_lowercase_1k_observation" in history
    assert "不能替代当前 v1beta" in history
    monkeypatch.setattr(docs.Path, "read_bytes", lambda _: b"{}")
    with pytest.raises(RuntimeError, match="evidence digest mismatch"):
        docs._render_banana_generate_content_history()


def test_missing_config_doc_settings_use_exact_source_authored_bodies(monkeypatch):
    bodies = {"contract_A": {"model": "A", "max_tokens": 256},
              "contract_B": {"model": "B", "max_tokens": 512}}
    monkeypatch.setattr(docs, "get_model_profile_catalog", lambda: SimpleNamespace(
        list_test_bindings=lambda *, contract_id, extension_type: [
            {"case_definitions": [{"case_id": "prefix_probe",
                                   "body" if contract_id == "contract_A" else "body_template": bodies[contract_id]}]}
        ]
    ))
    settings = docs._mpdb_case_settings("prefix_probe", set(bodies))
    assert settings == {"source_scoped_bodies": bodies}
    with pytest.raises(KeyError, match="source-authored"):
        docs._mpdb_case_settings("unregistered", set(bodies))


def test_lifecycle_documentation_keeps_operation_order_and_cleanup(monkeypatch):
    operations = [
        {"name": "parent", "method": "POST", "body": {"store": True}},
        {"name": "child", "method": "POST", "body": {"previous_interaction_id": "{owned_parent_id}"}},
        {"name": "delete_child", "method": "DELETE", "finally": True},
        {"name": "delete_parent", "method": "DELETE", "finally": True},
    ]
    monkeypatch.setattr(docs, "get_model_profile_catalog", lambda: SimpleNamespace(
        list_test_bindings=lambda **kwargs: [{"case_definitions": [{
            "case_id": "nonce_chain", "operations": operations,
            "generic_parameter_runner_supported": False, "limits": {"max_requests": 4},
        }]}],
    ))
    settings = docs._mpdb_case_settings("nonce_chain", {"source"})
    assert settings == {"operations": operations,
                        "generic_parameter_runner_supported": False,
                        "limits": {"max_requests": 4}}
    assert "parent:POST → child:POST → delete_child:DELETE" in docs._compact_settings(settings)
    settings["operations"][0]["body"]["store"] = False
    assert operations[0]["body"]["store"] is True


def test_interactions_docs_preserve_each_tool_mode_and_optional_summary():
    prefix = "gemini_3_7_flash_interactions_"
    assert "没有函数调用" in docs._validation_focus(prefix + "tools_none")
    assert "必须返回声明中的函数调用" in docs._validation_focus(prefix + "tools_any")
    for mode in ("auto", "validated"):
        assert "允许有效文本或" in docs._validation_focus(prefix + "tools_" + mode)
        assert "不执行外部工具或 follow-up" in docs._profile_purpose(prefix + "tools_" + mode, [])
    assert "摘要允许为空" in docs._validation_focus(prefix + "thinking_summaries_auto")
