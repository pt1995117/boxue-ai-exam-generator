#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT_DIR}"

TESTS=(
  "test_calculation_closure_alignment.py"
  "test_state_contract.py"
  "test_random_calc_type_guard.py"
  "test_writer_calc_log_regression.py"
  "test_locked_question_type_contract.py"
  "test_langgraph_flow_matrix.py::test_node_output_contracts_keep_required_question_state"
)

echo "[answer-type-regression] python compile check"
python -m py_compile exam_graph.py

echo "[answer-type-regression] running pytest"
pytest -q "${TESTS[@]}"

echo "[answer-type-regression] passed"
