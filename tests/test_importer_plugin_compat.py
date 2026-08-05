from typing import ClassVar

import pytest
from django.db import connection, models

from netbox_export.services.importer import _field_kwargs
from netbox_export.services.plugin_compat import PluginCompatibility
from netbox_export.services.references import ReferenceResolver

pytestmark = pytest.mark.django_db(transaction=True)


class Tenant(models.Model):
    class Meta:
        app_label = "tenancy"


class TenantManagedObject(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, null=True)

    class Meta:
        app_label = "compat_tests"


class Platform(models.Model):
    class Meta:
        app_label = "compat_tests"


class ConstrainedInstallation(models.Model):
    platform = models.ForeignKey(Platform, on_delete=models.PROTECT, null=True)

    class Meta:
        app_label = "compat_tests"
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(platform__isnull=False),
                name="compat_tests_installation_platform",
            )
        ]


class UniqueAddress(models.Model):
    class Meta:
        app_label = "compat_tests"


class UniqueAddressOwner(models.Model):
    primary_ip6 = models.OneToOneField(UniqueAddress, on_delete=models.SET_NULL, null=True)

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


def test_unresolved_relation_in_check_constraint_blocks_initial_save():
    resolver = ReferenceResolver({}, [], lambda label: None)
    record = {
        "fields": {},
        "relations": {"platform": {"ref": "compat_tests.platform:5"}},
    }

    kwargs, unresolved, unresolved_values, missing_required = _field_kwargs(
        ConstrainedInstallation,
        record,
        resolver,
        tenant_required=False,
    )

    assert kwargs is None
    assert unresolved == []
    assert unresolved_values == []
    assert missing_required == []


def test_resolved_nullable_unique_relation_is_deferred():
    address = UniqueAddress(pk=50)
    resolver = ReferenceResolver(
        {"compat_tests.uniqueaddress:50": address},
        [],
        lambda label: None,
    )
    record = {
        "fields": {},
        "relations": {"primary_ip6": {"ref": "compat_tests.uniqueaddress:50"}},
    }

    kwargs, unresolved, unresolved_values, missing_required = _field_kwargs(
        UniqueAddressOwner,
        record,
        resolver,
        tenant_required=False,
    )

    assert kwargs == {}
    assert unresolved == [("primary_ip6", {"ref": "compat_tests.uniqueaddress:50"})]
    assert unresolved_values == []
    assert missing_required == []


def test_existing_unique_relation_is_released_before_reassignment():
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(UniqueAddress)
        schema_editor.create_model(UniqueAddressOwner)
    try:
        address = UniqueAddress.objects.create()
        previous_owner = UniqueAddressOwner.objects.create(primary_ip6=address)
        imported_owner = UniqueAddressOwner()
        warnings = []
        compatibility = PluginCompatibility(warnings, dry_run=False, tenant_required=False)

        compatibility.release_unique_relation(imported_owner, "primary_ip6", address)
        imported_owner.primary_ip6 = address
        imported_owner.save()
        previous_owner.refresh_from_db()

        assert previous_owner.primary_ip6 is None
        assert imported_owner.primary_ip6 == address
        assert warnings == [
            (
                "Eindeutige Referenz compat_tests.uniqueaddressowner.primary_ip6 wurde von "
                f"Zielobjekt(en) {previous_owner.pk} gelöst und dem importierten Objekt neu zugeordnet."
            )
        ]
    finally:
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(UniqueAddressOwner)
            schema_editor.delete_model(UniqueAddress)
