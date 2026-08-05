from types import SimpleNamespace

from netbox_export.services.plugin_compat import relation_required_before_save


def relation_field(name, related_label):
    related_model = SimpleNamespace(_meta=SimpleNamespace(label_lower=related_label))
    return SimpleNamespace(name=name, remote_field=SimpleNamespace(model=related_model))


def test_tenant_relation_is_resolved_before_initial_save_when_required():
    field = relation_field("tenant", "tenancy.tenant")

    assert relation_required_before_save(field, tenant_required=True) is True


def test_tenant_relation_can_still_be_deferred_when_policy_is_disabled():
    field = relation_field("tenant", "tenancy.tenant")

    assert relation_required_before_save(field, tenant_required=False) is False


def test_unrelated_relation_is_not_affected_by_tenant_policy():
    field = relation_field("site", "dcim.site")

    assert relation_required_before_save(field, tenant_required=True) is False
