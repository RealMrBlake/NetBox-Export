from types import SimpleNamespace

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models

from netbox_export.services.plugin_compat import (
    MISSING_TENANT_MESSAGE,
    PluginCompatibility,
    check_constraint_field_names,
    is_module_relation,
    is_tenant_relation,
    relation_required_before_save,
    relation_should_be_deferred,
)


def relation_field(name="tenant", related_label="tenancy.tenant"):
    related_model = SimpleNamespace(_meta=SimpleNamespace(label_lower=related_label))
    return SimpleNamespace(name=name, remote_field=SimpleNamespace(model=related_model))


class FakeQuerySet:
    def __init__(self, result=None, exists=False):
        self.result = result
        self._exists = exists

    def order_by(self, *fields):
        return self

    def first(self):
        return self.result

    def exists(self):
        return self._exists


class FakeTenantManager:
    def __init__(self, existing=None):
        self.existing = existing
        self.created = []

    def filter(self, **lookup):
        if "name" in lookup:
            return FakeQuerySet(self.existing)
        return FakeQuerySet(exists=False)

    def create(self, **values):
        tenant = SimpleNamespace(pk=42, **values)
        self.created.append(tenant)
        return tenant


class FakeTenantModel:
    _default_manager = FakeTenantManager()


class FakeMeta:
    def get_field(self, name):
        if name != "tenant":
            raise FieldDoesNotExist(name)
        return relation_field()


class TenantRequiredObject:
    _meta = FakeMeta()

    def __init__(self):
        self.tenant_id = None
        self.save_calls = 0

    @property
    def tenant(self):
        return None

    @tenant.setter
    def tenant(self, value):
        self.tenant_id = value.pk

    def save(self, **kwargs):
        self.save_calls += 1
        if self.tenant_id is None:
            raise ValidationError(MISSING_TENANT_MESSAGE)


def test_identifies_tenant_relation():
    assert is_tenant_relation(relation_field()) is True
    assert is_tenant_relation(relation_field("site", "dcim.site")) is False


def test_identifies_module_component_relation():
    assert is_module_relation(relation_field("module", "dcim.module")) is True
    assert is_module_relation(relation_field("device", "dcim.device")) is False


def test_new_module_disables_automatic_component_replication():
    module = SimpleNamespace(_meta=SimpleNamespace(label_lower="dcim.module"))

    PluginCompatibility.prepare_initial_save(module, is_new=True)

    assert module._disable_replication is True


def test_existing_module_keeps_normal_save_behavior():
    module = SimpleNamespace(_meta=SimpleNamespace(label_lower="dcim.module"))

    PluginCompatibility.prepare_initial_save(module, is_new=False)

    assert not hasattr(module, "_disable_replication")


def test_identifies_relations_used_by_check_constraint():
    constraint = models.CheckConstraint(
        condition=(
            models.Q(device__isnull=False, virtualmachine__isnull=True)
            | models.Q(device__isnull=True, virtualmachine__isnull=False)
        ),
        name="platform",
    )
    model = type("ConstrainedModel", (), {"_meta": SimpleNamespace(constraints=[constraint])})
    field = relation_field("device", "dcim.device")
    field.model = model

    assert check_constraint_field_names(model) == frozenset({"device", "virtualmachine"})
    assert relation_required_before_save(field) is True


def test_nullable_unique_relation_is_deferred_unless_required_by_constraint():
    regular = relation_field("primary_ip6", "ipam.ipaddress")
    regular.null = True
    regular.unique = True
    regular.model = type(
        "VirtualMachine",
        (),
        {"_meta": SimpleNamespace(constraints=[])},
    )

    assert relation_should_be_deferred(regular) is True

    constrained = relation_field("platform", "dcim.device")
    constrained.null = True
    constrained.unique = True
    constraint = models.CheckConstraint(
        condition=models.Q(platform__isnull=False),
        name="platform_required",
    )
    constrained.model = type(
        "Installation",
        (),
        {"_meta": SimpleNamespace(constraints=[constraint])},
    )

    assert relation_should_be_deferred(constrained) is False


def test_creates_and_assigns_auto_import_tenant_when_policy_is_active():
    manager = FakeTenantManager()
    tenant_model = type("TenantModel", (), {"_default_manager": manager})
    warnings = []
    compatibility = PluginCompatibility(
        warnings,
        dry_run=False,
        tenant_required=True,
        tenant_model_loader=lambda: tenant_model,
    )
    obj = TenantRequiredObject()

    compatibility.save(obj)

    assert obj.tenant_id == 42
    assert obj.save_calls == 1
    assert len(manager.created) == 1
    assert warnings == [
        (
            'Der Mandant "Auto-Import" wurde automatisch erstellt und Objekten ohne '
            'auflösbaren Mandanten zugewiesen.'
        )
    ]


def test_validation_error_forces_fallback_for_older_plugin_versions():
    existing = SimpleNamespace(pk=7, name="Auto-Import", slug="auto-import")
    manager = FakeTenantManager(existing=existing)
    tenant_model = type("TenantModel", (), {"_default_manager": manager})
    warnings = []
    compatibility = PluginCompatibility(
        warnings,
        dry_run=False,
        tenant_required=False,
        tenant_model_loader=lambda: tenant_model,
    )
    obj = TenantRequiredObject()

    compatibility.save(obj)

    assert obj.tenant_id == 7
    assert obj.save_calls == 2
    assert manager.created == []
    assert warnings == [
        (
            'Der vorhandene Mandant "Auto-Import" wurde automatisch für Objekte ohne '
            'auflösbaren Mandanten ausgewählt.'
        )
    ]
