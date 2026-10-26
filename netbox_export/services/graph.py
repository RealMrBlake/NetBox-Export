from __future__ import annotations

from collections import defaultdict

from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext as _

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
PRIVATE_EXPORTABLE_MODELS = {
    # NetBox derives cable paths from these mappings, but marks the model private
    # because it has no public API of its own.
    "dcim.portmapping",
}

# Optional plugin models that extend a dependency (e.g. a device type) one-to-one.
# They are exported together with that dependency even though it is not a member.
DEPENDENCY_EXTENSION_MODELS = {
    # NetBox Utilities: disk bay template of a device type.
    "netbox_utilities.diskbaytemplate",
}


def is_exportable_model(model) -> bool:
    opts = model._meta
    return bool(
        opts.managed
        and not opts.abstract
        and not opts.proxy
        and not opts.auto_created
        and (
            not getattr(model, "_netbox_private", False)
            or opts.label_lower in PRIVATE_EXPORTABLE_MODELS
        )
        and opts.app_label not in EXCLUDED_APP_LABELS
        and opts.label_lower not in EXCLUDED_MODELS
    )


def exportable_models():
    return tuple(model for model in apps.get_models() if is_exportable_model(model))


def _descendants(obj):
    if hasattr(obj, "get_descendants"):
        return list(obj.get_descendants(include_self=True))
    return [obj]


def _chunks(values, size):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


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
    """Collect scoped objects and dependencies using batched relation queries."""

    def __init__(self, max_objects: int, query_batch_size: int = 500):
        self.max_objects = max_objects
        self.query_batch_size = max(1, query_batch_size)
        self.members: dict[str, models.Model] = {}
        self.dependencies: dict[str, models.Model] = {}
        self._custom_field_cache = {}
        self._models = exportable_models()
        (
            self._reverse_fields,
            self._generic_fields,
            self._peer_fields,
            self._extension_fields,
        ) = self._build_relation_indexes()

    @property
    def objects(self) -> dict[str, models.Model]:
        return {**self.members, **self.dependencies}

    def _build_relation_indexes(self):
        reverse_fields = defaultdict(lambda: defaultdict(list))
        generic_fields = defaultdict(list)
        peer_fields = defaultdict(list)
        extension_fields = defaultdict(list)
        for candidate_model in self._models:
            for relation_field in candidate_model._meta.concrete_fields:
                if not isinstance(relation_field, (models.ForeignKey, models.OneToOneField)):
                    continue
                related_model = relation_field.remote_field.model
                if (
                    relation_field.remote_field.on_delete in (models.CASCADE, models.PROTECT)
                    or relation_field.name in SCOPE_LINK_FIELDS
                ):
                    reverse_fields[related_model][candidate_model].append(relation_field)
                if related_model._meta.label_lower in PEER_CONTAINER_MODELS:
                    peer_fields[candidate_model].append(relation_field)
                if (
                    candidate_model._meta.label_lower in DEPENDENCY_EXTENSION_MODELS
                    and isinstance(relation_field, models.OneToOneField)
                ):
                    extension_fields[related_model].append((candidate_model, relation_field))
            for generic_field in candidate_model._meta.private_fields:
                if isinstance(generic_field, GenericForeignKey):
                    generic_fields[candidate_model].append(generic_field)
        return reverse_fields, generic_fields, peer_fields, extension_fields

    def _check_limit(self):
        object_count = len(self.members) + len(self.dependencies)
        if object_count > self.max_objects:
            raise GraphLimitError(
                _(
                    "The export would contain more than %(max)s objects. "
                    "Narrow the scope or increase max_objects."
                ) % {"max": self.max_objects}
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

    def _group_by_model(self, objects):
        grouped = defaultdict(list)
        for obj in objects:
            grouped[type(obj)].append(obj)
        return grouped

    def _expand_members(self, seeds):
        pending = {}
        for obj in seeds:
            if self.add_member(obj):
                pending[object_key(obj)] = obj

        while pending:
            next_pending = {}
            for parent_model, parents in self._group_by_model(pending.values()).items():
                parent_ct = ContentType.objects.get_for_model(parent_model)
                direct_candidates = self._reverse_fields.get(parent_model, {})
                peer_fields = self._peer_fields.get(parent_model, [])
                for parent_batch in _chunks(parents, self.query_batch_size):
                    parent_ids = [obj.pk for obj in parent_batch]

                    if peer_fields:
                        peer_names = [field.name for field in peer_fields]
                        queryset = parent_model._default_manager.filter(pk__in=parent_ids).select_related(*peer_names)
                        for parent in queryset.iterator(chunk_size=self.query_batch_size):
                            for relation_field in peer_fields:
                                related = getattr(parent, relation_field.name, None)
                                if related is not None and self.add_member(related):
                                    next_pending[object_key(related)] = related

                    candidate_queries = defaultdict(models.Q)
                    for candidate_model, relation_fields in direct_candidates.items():
                        for relation_field in relation_fields:
                            candidate_queries[candidate_model] |= models.Q(**{f"{relation_field.attname}__in": parent_ids})
                    for candidate_model, generic_fields in self._generic_fields.items():
                        for generic_field in generic_fields:
                            candidate_queries[candidate_model] |= models.Q(
                                **{
                                    generic_field.ct_field: parent_ct,
                                    f"{generic_field.fk_field}__in": parent_ids,
                                }
                            )

                    for candidate_model, query in candidate_queries.items():
                        queryset = candidate_model._default_manager.filter(query).distinct()
                        for child in queryset.iterator(chunk_size=self.query_batch_size):
                            if self.add_member(child):
                                next_pending[object_key(child)] = child
            pending = next_pending

    def custom_fields_for_model(self, model):
        if model not in self._custom_field_cache:
            if hasattr(model, "custom_field_data"):
                from extras.models import CustomField

                self._custom_field_cache[model] = list(CustomField.objects.get_for_model(model))
            else:
                self._custom_field_cache[model] = []
        return self._custom_field_cache[model]

    def _load_objects(self, model, objects):
        foreign_keys = [
            field.name
            for field in model._meta.concrete_fields
            if isinstance(field, (models.ForeignKey, models.OneToOneField))
        ]
        prefetch_fields = [field.name for field in model._meta.many_to_many]
        prefetch_fields.extend(
            field.name for field in model._meta.private_fields if isinstance(field, GenericForeignKey)
        )
        for object_batch in _chunks(objects, self.query_batch_size):
            object_ids = [obj.pk for obj in object_batch]
            queryset = model._default_manager.filter(pk__in=object_ids).order_by("pk")
            if foreign_keys:
                # select_related() without arguments is deprecated since Django 6.1.
                queryset = queryset.select_related(*foreign_keys)
            if prefetch_fields:
                queryset = queryset.prefetch_related(*prefetch_fields)
            yield from queryset.iterator(chunk_size=self.query_batch_size)

    def iter_loaded_objects(self):
        grouped = self._group_by_model(self.objects.values())
        for model in sorted(grouped, key=lambda item: item._meta.label_lower):
            yield from self._load_objects(model, grouped[model])

    def _related_objects(self, obj, custom_fields):
        related_objects = []
        for relation_field in obj._meta.concrete_fields:
            if isinstance(relation_field, (models.ForeignKey, models.OneToOneField)):
                related = getattr(obj, relation_field.name, None)
                if related is not None:
                    related_objects.append(related)
        for generic_field in obj._meta.private_fields:
            if isinstance(generic_field, GenericForeignKey):
                related = getattr(obj, generic_field.name, None)
                if related is not None:
                    related_objects.append(related)
        for many_to_many_field in obj._meta.many_to_many:
            try:
                related_objects.extend(getattr(obj, many_to_many_field.name).all())
            except (AttributeError, TypeError):
                pass

        related_objects.extend(custom_fields)
        return related_objects

    def _custom_value_dependencies(self, objects, custom_fields):
        target_ids_by_model = defaultdict(set)
        for custom_field in custom_fields:
            if custom_field.type not in ("object", "multiobject") or not custom_field.related_object_type:
                continue
            target_model = custom_field.related_object_type.model_class()
            for obj in objects:
                raw_value = obj.custom_field_data.get(custom_field.name)
                if raw_value in (None, "", []):
                    continue
                values = raw_value if custom_field.type == "multiobject" else [raw_value]
                target_ids_by_model[target_model].update(values)

        for obj in objects:
            if (
                obj._meta.label_lower == "extras.customfield"
                and obj.type in ("object", "multiobject")
                and obj.related_object_type
                and obj.default not in (None, "", [])
            ):
                target_model = obj.related_object_type.model_class()
                values = obj.default if obj.type == "multiobject" else [obj.default]
                target_ids_by_model[target_model].update(values)

        for target_model, target_ids in target_ids_by_model.items():
            for target_id_batch in _chunks(target_ids, self.query_batch_size):
                yield from target_model._default_manager.filter(pk__in=target_id_batch).iterator(
                    chunk_size=self.query_batch_size
                )

    def _extension_objects(self, model, objects):
        object_ids = [obj.pk for obj in objects]
        for extension_model, relation_field in self._extension_fields.get(model, ()):
            for id_batch in _chunks(object_ids, self.query_batch_size):
                yield from extension_model._default_manager.filter(
                    **{f"{relation_field.attname}__in": id_batch}
                ).iterator(chunk_size=self.query_batch_size)

    def _collect_dependencies(self):
        pending = dict(self.members)
        scanned = set()
        while pending:
            next_pending = {}
            for model, objects in self._group_by_model(pending.values()).items():
                custom_fields = self.custom_fields_for_model(model)
                loaded_objects = list(self._load_objects(model, objects))
                for extension in self._extension_objects(model, loaded_objects):
                    if self.add_dependency(extension):
                        next_pending[object_key(extension)] = extension
                for related in self._custom_value_dependencies(loaded_objects, custom_fields):
                    if self.add_dependency(related):
                        next_pending[object_key(related)] = related
                for obj in loaded_objects:
                    key = object_key(obj)
                    if key in scanned:
                        continue
                    scanned.add(key)
                    for related in self._related_objects(obj, custom_fields):
                        if self.add_dependency(related):
                            next_pending[object_key(related)] = related
            pending = next_pending

    def collect(self, seeds):
        self._expand_members(seeds)
        self._collect_dependencies()
        return self
