import admin_api


def test_skip_fuse_for_writer_quality_family_in_large_batch():
    assert admin_api._should_skip_fuse_for_error(
        error_key="critic:writer_quality_family",
        target_question_count=200,
    )


def test_do_not_skip_fuse_for_writer_quality_family_in_small_batch():
    assert not admin_api._should_skip_fuse_for_error(
        error_key="critic:writer_quality_family",
        target_question_count=20,
    )


def test_do_not_skip_fuse_for_other_error_key():
    assert not admin_api._should_skip_fuse_for_error(
        error_key="critic:reverse_solve_fail",
        target_question_count=200,
    )


def test_skip_fuse_for_per_question_loop_fused():
    assert admin_api._should_skip_fuse_for_error(
        error_key="critic:per_question_loop_fused",
        target_question_count=10,
    )
