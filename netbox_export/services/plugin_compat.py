from __future__ import annotations

from functools import cache

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models

AUTO_IMPORT_TENANT_NAME = "Auto-Import"
AUTO_IMPORT_TENANT_SLUG = "auto-import"
MISSING_TENANT_MESSAGE = "Für dieses Objekt muss ein Mandant angegeben werden."


def netbox_utilities_tenant_required() -> bool:
    """Return the effective tenant policy without requiring the optional plugin."""
    try:
        from netbox_utilities.runtime import tenant_required
    except ImportError:
        return False
    return bool(tenant_required())


def is_tenant_relation(field) -> bool:
    if field.name != "tenant":
        return False
    related_model = getattr(field.remote_field, "model", None)
    return getattr(getattr(related_model, "_meta", None), "label_lower", None) == "tenancy.tenant"


def _condition_field_names(condition):
    for child in getattr(condition, "children", ()):
        if isinstance(child, tuple):
            yield child[0].split("__", 1)[0]
        else:
            yield from _condition_field_names(child)


@cache
def check_constraint_field_names(model) -> frozenset[str]:
    if model is None:
        return frozenset()
    names = set()
    for constraint in model._meta.constraints:
        if isinstance(constraint, models.CheckConstraint):
            names.update(_condition_field_names(constraint.condition))
    return frozenset(names)


def relation_required_before_save(field) -> bool:
    return is_tenant_relation(field) or field.name in check_constraint_field_names(
        getattr(field, "model", None)
    )


class PluginCompatibility:
    def __init__(
        self,
        warnings,
        *,
        dry_run: bool,
        tenant_required: bool | None = None,
        tenant_model_loader=None,
    ):
        self.warnings = warnings
        self.dry_run = dry_run
        self.tenant_required = (
            netbox_utilities_tenant_required() if tenant_required is None else tenant_required
        )
        self.tenant_model_loader = tenant_model_loader or (lambda: apps.get_model("tenancy.tenant"))
        self._fallback_tenant = None

    @staticmethod
    def _supports_tenant(obj) -> bool:
        try:
            field = obj._meta.get_field("tenant")
        except FieldDoesNotExist:
            return False
        return is_tenant_relation(field)

    @staticmethod
    def _is_missing_tenant_error(exc: ValidationError) -> bool:
        return MISSING_TENANT_MESSAGE in exc.messages

    def _auto_import_tenant(self):
        if self._fallback_tenant is not None:
            return self._fallback_tenant

        model = self.tenant_model_loader()
        tenant = model._default_manager.filter(name=AUTO_IMPORT_TENANT_NAME).order_by("pk").first()
        created = tenant is None
        if tenant is None:
            slug = AUTO_IMPORT_TENANT_SLUG
            suffix = 2
            while model._default_manager.filter(slug=slug).exists():
                slug = f"{AUTO_IMPORT_TENANT_SLUG}-{suffix}"
                suffix += 1
            tenant = model._default_manager.create(name=AUTO_IMPORT_TENANT_NAME, slug=slug)

        if self.dry_run:
            message = (
                'Für den Prüflauf wurde der Mandant "Auto-Import" automatisch als Ersatzmandant '
                "verwendet; er wird nicht dauerhaft angelegt oder geändert."
            )
        elif created:
            message = (
                'Der Mandant "Auto-Import" wurde automatisch erstellt und Objekten ohne '
                "auflösbaren Mandanten zugewiesen."
            )
        else:
            message = (
                'Der vorhandene Mandant "Auto-Import" wurde automatisch für Objekte ohne '
                "auflösbaren Mandanten ausgewählt."
            )
        self.warnings.append(message)
        self._fallback_tenant = tenant
        return tenant

    def _assign_fallback_tenant(self, obj, *, force: bool = False) -> bool:
        if getattr(obj, "tenant_id", None) is not None or not self._supports_tenant(obj):
            return False
        if not force and not self.tenant_required:
            return False
        obj.tenant = self._auto_import_tenant()
        return True

    def save(self, obj, **kwargs):
        self._assign_fallback_tenant(obj)
        try:
            return obj.save(**kwargs)
        except ValidationError as exc:
            if not self._is_missing_tenant_error(exc) or not self._assign_fallback_tenant(obj, force=True):
                raise
            return obj.save(**kwargs)
