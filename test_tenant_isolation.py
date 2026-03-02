from tenant_context import assert_tenant_access
from tenants_config import tenant_bank_path


def test_teacher_hz_can_access_hz():
    assert_tenant_access("teacher_hz", "hz")


def test_teacher_hz_cannot_access_bj():
    try:
        assert_tenant_access("teacher_hz", "bj")
        assert False, "teacher_hz should not access bj"
    except PermissionError as e:
        assert str(e) == "TENANT_FORBIDDEN"


def test_tenant_bank_path_isolated():
    assert str(tenant_bank_path("hz")) != str(tenant_bank_path("bj"))
