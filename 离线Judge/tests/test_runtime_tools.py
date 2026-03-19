from langchain_core.runnables import RunnableLambda

from src.agents.safe_python_runner import execute_code, execute_generate_possible_answers
from src.llm.client import ReliableLLMClient, get_observability, reset_observability


def test_safe_python_runner_executes_json_emit():
    code = 'computed_answer="A"\n__judge_emit({"ok": True, "computed_answer": computed_answer, "distractor_analysis": {}})'
    result = execute_code(code)
    assert result["ok"] is True
    assert result["result"]["computed_answer"] == "A"


def test_safe_python_runner_blocks_forbidden_import():
    code = 'import os\n__judge_emit({"ok": True})'
    result = execute_code(code)
    assert result["ok"] is False
    assert any("禁止导入模块" in x for x in result["issues"])


def test_reliable_llm_client_fallback_on_invalid_json():
    llm = RunnableLambda(lambda _x: "not-json")
    client = ReliableLLMClient(llm, timeout_seconds=1, retries=0)
    data = client.invoke_json("prompt", fallback={"passed": True, "issues": []})
    assert data["passed"] is True


def test_execute_generate_possible_answers_success():
    code = (
        "def generate_possible_answers(context):\n"
        "    return [\n"
        "        {'type': 'correct', 'value': 100},\n"
        "        {'type': 'error_wrong_rate', 'value': 90},\n"
        "        {'type': 'error_forgot_vat', 'value': 80},\n"
        "    ]\n"
    )
    result = execute_generate_possible_answers(code, {"foo": "bar"})
    assert result["ok"] is True
    assert isinstance(result["result"], list)


def test_execute_generate_possible_answers_requires_function():
    code = "x = 1"
    result = execute_generate_possible_answers(code, {})
    assert result["ok"] is False
    assert any("generate_possible_answers" in x for x in result["issues"])


def test_reliable_llm_client_collects_observability_metrics():
    class FakeResp:
        content = '{"ok": true}'
        usage_metadata = {"input_tokens": 12, "output_tokens": 5}

    reset_observability()
    llm = RunnableLambda(lambda _x: FakeResp())
    client = ReliableLLMClient(llm, timeout_seconds=1, retries=0)
    _ = client.invoke_text("prompt")
    obs = get_observability()
    assert obs["prompt_tokens"] >= 12
    assert obs["completion_tokens"] >= 5
    assert obs["latency_ms"] >= 0


def test_reliable_llm_client_does_not_double_count_usage_when_both_metadata_exist():
    class FakeResp:
        content = '{"ok": true}'
        usage_metadata = {"input_tokens": 12, "output_tokens": 5}
        response_metadata = {"token_usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}

    reset_observability()
    llm = RunnableLambda(lambda _x: FakeResp())
    client = ReliableLLMClient(llm, timeout_seconds=1, retries=0)
    _ = client.invoke_text("prompt")
    obs = get_observability()
    assert obs["prompt_tokens"] == 12
    assert obs["completion_tokens"] == 5
