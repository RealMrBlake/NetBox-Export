import uuid
from typing import ClassVar

import pytest
from django.db import connection, models

from netbox_export.services import importer as importer_module
from netbox_export.services.importer import (
    _field_kwargs,
    _find_existing,
    _identity_lookup,
)
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


class Device(models.Model):
    class Meta:
        app_label = "dcim"


class RearPort(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    name = models.CharField(max_length=64)

    class Meta:
        app_label = "dcim"
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=("device", "name"),
                name="dcim_rearport_unique_device_name",
            )
        ]


class Module(models.Model):
    class Meta:
        app_label = "dcim"


class ModuleBay(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    module = models.ForeignKey(Module, on_delete=models.CASCADE)
    name = models.CharField(max_length=64)

    class Meta:
        app_label = "dcim"


class ModularRearPort(models.Model):
    module = models.ForeignKey(Module, on_delete=models.SET_NULL, null=True)

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


def test_modular_component_waits_for_module_before_initial_save():
    resolver = ReferenceResolver({}, [], lambda label: None)
    record = {
        "fields": {},
        "relations": {"module": {"ref": "dcim.module:12"}},
    }

    kwargs, unresolved, unresolved_values, missing_required = _field_kwargs(
        ModularRearPort,
        record,
        resolver,
        tenant_required=False,
    )

    assert kwargs is None
    assert unresolved == []
    assert unresolved_values == []
    assert missing_required == []


def test_module_bay_identity_includes_owning_module():
    device = Device(pk=210)
    module = Module(pk=12)
    resolver = ReferenceResolver(
        {
            "dcim.device:210": device,
            "dcim.module:12": module,
        },
        [],
        lambda label: None,
    )
    record = {
        "fields": {"name": "3-LC"},
        "relations": {
            "device": {"ref": "dcim.device:210"},
            "module": {"ref": "dcim.module:12"},
        },
    }

    lookup = _identity_lookup(ModuleBay, record, resolver)

    assert lookup == {"device": device, "module": module, "name": "3-LC"}


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
                "Unique reference compat_tests.uniqueaddressowner.primary_ip6 was released from target "
                f"object(s) {previous_owner.pk} and reassigned to the imported object."
            )
        ]
    finally:
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(UniqueAddressOwner)
            schema_editor.delete_model(UniqueAddress)


def test_natural_key_rebinds_stale_mapping_instead_of_creating_duplicate(monkeypatch):
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(Device)
        schema_editor.create_model(RearPort)
    try:
        device = Device.objects.create()
        natural = RearPort.objects.create(device=device, name="3-LC")
        stale_mapping_target = RearPort(pk=999, device=device, name="Alt")
        monkeypatch.setattr(
            importer_module,
            "_mapped_object",
            lambda source_instance, record, model: stale_mapping_target,
        )
        warnings = []
        resolver = ReferenceResolver(
            {"dcim.device:160": device},
            warnings,
            lambda label: None,
        )
        record = {
            "id": "dcim.rearport:77",
            "model": "dcim.rearport",
            "source_pk": "77",
            "fields": {"name": "3-LC"},
            "relations": {"device": {"ref": "dcim.device:160"}},
        }

        existing = _find_existing(uuid.uuid4(), RearPort, record, resolver)

        assert existing == natural
        assert warnings == [
            (
                "Stored mapping for dcim.rearport:77 was corrected from target ID 999 to "
                f"target ID {natural.pk} because the natural key "
                f"{{'device': <Device: Device object ({device.pk})>, 'name': '3-LC'}} already exists."
            )
        ]
    finally:
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(RearPort)
            schema_editor.delete_model(Device)
