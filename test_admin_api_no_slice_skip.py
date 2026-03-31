import inspect

from admin_api import api_generate_questions, api_generate_questions_stream


def test_generate_api_no_longer_downgrades_and_skips_slices():
    source = inspect.getsource(api_generate_questions)
    assert "切片降权跳过" not in source
    assert "skipped_slice_ids" not in source


def test_generate_stream_api_no_longer_downgrades_and_skips_slices():
    source = inspect.getsource(api_generate_questions_stream)
    assert "切片降权跳过" not in source
    assert "skipped_slice_ids" not in source
