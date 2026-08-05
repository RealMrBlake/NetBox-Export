from __future__ import annotations

from collections import deque

from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from .codec import object_key
from .exceptions import GraphLimitError

EXCLUDED_APP_LABELS = {
    "account",
    "admin",
    "auth",
    "contenttypes",
    "sessions",
    "users",
    "netbox_export",
}
EXCLUDED_MODELS = {
    "core.job",
    "core.objectchange",
    "extras.eventrule",
    "extras.journalentry",
    "extras.notification",
    "extras.notificationgroup",
    "extras.savedfilter",
    "extras.subscription",
}
SCOPE_LINK_FIELDS = {"tenant", "site", "location", "region"}
PEER_CONTAINER_MODELS = {"dcim.cable", "circuits.circuit", "circuits.virtualcircuit"}


def is_exportable_model(model) -> bool:
    opts = model._meta
    return bool(
        opts.managed
        and not opts.abstract
        and not opts.proxy
        and not opts.auto_created
        and opts.app_label not in EXCLUDED_APP_LABELS
        and opts.label_lower not in EXCLUDED_MODELS
    )


def exportable_models():
    return tuple(model for model in apps.get_models() if is_exportable_model(model))


def _descendants(obj):
    if hasattr(obj, "get_descendants"):
        return list(obj.get_descendants(include_self=True))
    return [obj]


def seed_scope(scope_type: str, scope_id: int):
    from dcim.models import Location, Region, Site
    from tenancy.models import Tenant, TenantGroup

    models_by_scope = {
        "tenant_group": TenantGroup,
        "tenant": Tenant,
        "region": Region,
        "site": Site,
        "location": Location,
    }
    model = models_by_scope[scope_type]
    root = model.objects.get(pk=scope_id)
    seeds = _descendants(root)

    if scope_type == "tenant_group":
        group_ids = [obj.pk for obj in seeds]
        seeds.extend(Tenant.objects.filter(group_id__in=group_ids))
    elif scope_type == "region":
        region_ids = [obj.pk for obj in seeds]
        seeds.extend(Site.objects.filter(region_id__in=region_ids))
    return root, seeds


class ObjectGraph:
    """Collect scoped objects first and their forward dependencies second."""

    def __init__(self, max_objects: int):
        self.max_objects = max_objects
        self.members: dict[str, models.Model] = {}
        self.dependencies: dict[str, models.Model] = {}

    @property
    def objects(self) -> dict[str, models.Model]:
        return {**self.members, **self.dependencies}

    def _check_limit(self):
        if len(self.objects) > self.max_objects:
            raise GraphLimitError(
                f"Der Export würde mehr als {self.max_objects} Objekte enthalten. "
                "Bitte den Bereich verkleinern oder max_objects erhöhen."
            )

    def add_member(self, obj) -> bool:
        key = object_key(obj)
        if key in self.members:
            return False
        self.dependencies.pop(key, None)
        self.members[key] = obj
        self._check_limit()
        return True

    def add_dependency(self, obj) -> bool:
        key = object_key(obj)
        if key in self.members or key in self.dependencies or not is_exportable_model(type(obj)):
            return False
        self.dependencies[key] = obj
        self._check_limit()
        return True

    def collect(self, seeds):
        queue = deque()
        for obj in seeds:
            if self.add_member(obj):
                queue.append(obj)

        models_to_scan = exportable_models()
        while True:
            while queue:
                parent = queue.popleft()
                parent_model = type(parent)
                parent_ct = ContentType.objects.get_for_model(parent_model)
                for candidate_model in models_to_scan:
                    query = models.Q()
                    for field in candidate_model._meta.concrete_fields:
                        if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
                            continue
                        if field.remote_field.model is not parent_model:
                            continue
                        if (
                            field.remote_field.on_delete not in (models.CASCADE, models.PROTECT)
                            and field.name not in SCOPE_LINK_FIELDS
                        ):
                            continue
                        query |= models.Q(**{field.attname: parent.pk})
                    for field in candidate_model._meta.private_fields:
                        if isinstance(field, GenericForeignKey):
                            query |= models.Q(**{field.ct_field: parent_ct, field.fk_field: parent.pk})
                    if not query:
                        continue
                    for child in candidate_model.objects.filter(query).distinct().iterator():
                        if self.add_member(child):
                            queue.append(child)

            promoted = False
            for obj in list(self.members.values()):
                for field in obj._meta.concrete_fields:
                    if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
                        continue
                    related = getattr(obj, field.name, None)
                    if related is None or related._meta.label_lower not in PEER_CONTAINER_MODELS:
                        continue
                    if self.add_member(related):
                        queue.append(related)
                        promoted = True
            if not promoted:
                break

        dependency_queue = deque(self.members.values())
        scanned = set()
        while dependency_queue:
            obj = dependency_queue.popleft()
            key = object_key(obj)
            if key in scanned:
                continue
            scanned.add(key)
            related_objects = []
            for field in obj._meta.concrete_fields:
                if isinstance(field, (models.ForeignKey, models.OneToOneField)):
                    related = getattr(obj, field.name, None)
                    if related is not None:
                        related_objects.append(related)
            for field in obj._meta.private_fields:
                if isinstance(field, GenericForeignKey):
                    related = getattr(obj, field.name, None)
                    if related is not None:
                        related_objects.append(related)
            for field in obj._meta.many_to_many:
                try:
                    related_objects.extend(getattr(obj, field.name).all())
                except (AttributeError, TypeError):
                    pass
            if hasattr(obj, "custom_field_data"):
                from extras.models import CustomField

                custom_fields = list(CustomField.objects.get_for_model(type(obj)))
                related_objects.extend(custom_fields)
                for custom_field in custom_fields:
                    if custom_field.type not in ("object", "multiobject") or not custom_field.related_object_type:
                        continue
                    raw_value = obj.custom_field_data.get(custom_field.name)
                    if raw_value in (None, "", []):
                        continue
                    target_model = custom_field.related_object_type.model_class()
                    target_ids = raw_value if custom_field.type == "multiobject" else [raw_value]
                    related_objects.extend(target_model._default_manager.filter(pk__in=target_ids))
            if (
                obj._meta.label_lower == "extras.customfield"
                and obj.type in ("object", "multiobject")
                and obj.related_object_type
                and obj.default not in (None, "", [])
            ):
                target_model = obj.related_object_type.model_class()
                target_ids = obj.default if obj.type == "multiobject" else [obj.default]
                related_objects.extend(target_model._default_manager.filter(pk__in=target_ids))
            for related in related_objects:
                if self.add_dependency(related):
                    dependency_queue.append(related)
        return self
