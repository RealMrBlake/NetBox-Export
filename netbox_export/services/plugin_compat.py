from __future__ import annotations


def netbox_utilities_tenant_required() -> bool:
    """Return the effective tenant policy without requiring the optional plugin."""
    try:
        from netbox_utilities.runtime import tenant_required
    except ImportError:
        return False
    return bool(tenant_required())


def relation_required_before_save(field, *, tenant_required: bool) -> bool:
    if not tenant_required or field.name != "tenant":
        return False
    related_model = getattr(field.remote_field, "model", None)
    return getattr(getattr(related_model, "_meta", None), "label_lower", None) == "tenancy.tenant"
