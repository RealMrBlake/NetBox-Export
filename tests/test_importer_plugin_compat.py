from django.db import models

from netbox_export.services.importer import _field_kwargs
from netbox_export.services.references import ReferenceResolver


class Tenant(models.Model):
    class Meta:
        app_label = "tenancy"


class TenantManagedObject(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, null=True)

    class Meta:
        app_label = "compat_tests"


def tenant_record():
    return {
        "fields": {},
        "relations": {"tenant": {"ref": "tenancy.tenant:23"}},
    }


def test_unresolved_tenant_always_blocks_initial_save():
    resolver = ReferenceResolver({}, [], lambda label: None)

    kwargs, unresolved, unresolved_values, missing_required = _field_kwargs(
        TenantManagedObject,
        tenant_record(),
        resolver,
        tenant_required=True,
    )

    assert kwargs is None
    assert unresolved == []
    assert unresolved_values == []
    assert missing_required == []


def test_unresolved_tenant_also_blocks_when_policy_detection_is_unavailable():
    resolver = ReferenceResolver({}, [], lambda label: None)

    kwargs, unresolved, unresolved_values, missing_required = _field_kwargs(
        TenantManagedObject,
        tenant_record(),
        resolver,
        tenant_required=False,
    )

    assert kwargs is None
    assert unresolved == []
    assert unresolved_values == []
    assert missing_required == []
