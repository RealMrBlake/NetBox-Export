from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import IntegrityError, models, transaction

from netbox_export.models import ImportedObjectMapping

from .archive import ParsedArchive
from .codec import SKIP_FIELD_NAMES, decode_scalar, generic_foreign_keys
from .exceptions import ArchiveValidationError, ExportImportError, ImportConflictError
from .plugin_compat import (
    PluginCompatibility,
    is_tenant_relation,
    relation_required_before_save,
)
from .references import MISSING_REFERENCE, ReferenceResolver

EXPLICIT_IDENTITIES = {
    "tenancy.tenantgroup": ("slug",),
    "tenancy.tenant": ("group", "slug"),
    "dcim.region": ("parent", "slug"),
    "dcim.sitegroup": ("parent", "slug"),
    "dcim.site": ("slug",),
    "dcim.location": ("site", "parent", "slug"),
    "dcim.rack": ("site", "location", "name"),
    "dcim.device": ("site", "tenant", "name"),
    "dcim.interface": ("device", "name"),
    "dcim.consoleport": ("device", "name"),
    "dcim.consoleserverport": ("device", "name"),
    "dcim.powerport": ("device", "name"),
    "dcim.poweroutlet": ("device", "name"),
    "dcim.devicebay": ("device", "name"),
    "dcim.modulebay": ("device", "name"),
    "dcim.inventoryitem": ("device", "parent", "name"),
    "ipam.prefix": ("vrf", "prefix"),
    "ipam.ipaddress": ("vrf", "address"),
    "ipam.vlan": ("group", "vid"),
    "circuits.circuit": ("provider", "cid"),
    "virtualization.virtualmachine": ("cluster", "tenant", "name"),
    "virtualization.vminterface": ("virtual_machine", "name"),
}


class IdentityNotReady(Exception):
    pass


logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    dry_run: bool
    created: int = 0
    updated: int = 0
    skipped: int = 0
    mapped: int = 0
    models: dict[str, dict[str, int]] = dataclass_field(default_factory=dict)
    warnings: list[str] = dataclass_field(default_factory=list)

    def add(self, model: str, action: str):
        setattr(self, action, getattr(self, action) + 1)
        counters = self.models.setdefault(model, {"created": 0, "updated": 0, "skipped": 0})
        counters[action] += 1


def _model_for(label: str):
    try:
        model = apps.get_model(label)
    except (LookupError, ValueError) as exc:
        raise ArchiveValidationError(f"Das Modell {label} ist auf der Zielinstanz nicht installiert.") from exc
    if model is None:
        raise ArchiveValidationError(f"Das Modell {label} ist auf der Zielinstanz nicht installiert.")
    return model


def _decode_archived_value(encoded, resolver):
    if isinstance(encoded, dict) and encoded.get("$type") == "object_ref":
        target, available = resolver.resolve(encoded.get("value"))
        if target is MISSING_REFERENCE:
            return None, True
        return (target.pk if available and target is not None else None), available
    if isinstance(encoded, dict) and encoded.get("$type") == "multiobject_ref":
        values = []
        for spec in encoded.get("value", []):
            target, available = resolver.resolve(spec)
            if not available:
                return None, False
            if target is MISSING_REFERENCE:
                continue
            values.append(target.pk)
        return values, True
    if isinstance(encoded, dict) and "$type" not in encoded:
        value = {}
        all_available = True
        for key, item in encoded.items():
            decoded, available = _decode_archived_value(item, resolver)
            value[key] = decoded
            all_available &= available
        return value, all_available
    return decode_scalar(encoded), True


def _identity_candidates(model):
    explicit = EXPLICIT_IDENTITIES.get(model._meta.label_lower)
    if explicit:
        yield explicit
    for field in model._meta.concrete_fields:
        if field.unique and not field.primary_key:
            yield (field.name,)
    if model._meta.unique_together:
        yield from model._meta.unique_together
    for constraint in model._meta.constraints:
        if isinstance(constraint, models.UniqueConstraint) and constraint.fields:
            yield tuple(constraint.fields)


def _identity_lookup(model, record, resolver):
    scalar_values = record.get("fields", {})
    relation_values = record.get("relations", {})
    generic_values = record.get("generic_relations", {})
    generic_storage = {}
    for generic_field in generic_foreign_keys(model):
        if generic_field.name not in generic_values:
            continue
        spec = generic_values[generic_field.name]
        generic_storage[generic_field.ct_field] = ("content_type", spec)
        generic_storage[generic_field.fk_field] = ("object_id", spec)
    for candidate in _identity_candidates(model):
        lookup = {}
        usable = True
        for name in candidate:
            if name in scalar_values:
                value = decode_scalar(scalar_values[name])
            elif name in relation_values:
                value, available = resolver.resolve(relation_values[name])
                if not available:
                    raise IdentityNotReady
                if value is MISSING_REFERENCE:
                    usable = False
                    break
            elif name in generic_storage:
                value_type, spec = generic_storage[name]
                target, available = resolver.resolve(spec)
                if not available:
                    raise IdentityNotReady
                if target is MISSING_REFERENCE:
                    usable = False
                    break
                if target is None:
                    value = None
                elif value_type == "content_type":
                    value = ContentType.objects.get_for_model(target, for_concrete_model=False)
                else:
                    value = target.pk
            else:
                usable = False
                break
            if value is None and len(candidate) == 1:
                usable = False
                break
            lookup[name] = value
        if usable:
            return lookup
    return None


def _mapped_object(source_instance: uuid.UUID, record: dict, model):
    mapping = ImportedObjectMapping.objects.filter(
        source_instance=source_instance,
        source_model=record["model"],
        source_object_id=record["source_pk"],
    ).first()
    if not mapping:
        return None
    try:
        return model._default_manager.get(pk=mapping.target_id)
    except model.DoesNotExist:
        mapping.delete()
        return None


def _find_existing(source_instance, model, record, resolver):
    mapped = _mapped_object(source_instance, record, model)
    if mapped is not None:
        return mapped
    lookup = _identity_lookup(model, record, resolver)
    if not lookup:
        return None
    try:
        return model._default_manager.get(**lookup)
    except model.DoesNotExist:
        return None
    except model.MultipleObjectsReturned as exc:
        raise ImportConflictError(
            f"Mehrere Zielobjekte passen auf {record['model']} mit {lookup}."
        ) from exc


def _write_mapping(source_instance, record, obj):
    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    ImportedObjectMapping.objects.update_or_create(
        source_instance=source_instance,
        source_model=record["model"],
        source_object_id=record["source_pk"],
        defaults={"target_type": content_type, "target_id": str(obj.pk)},
    )


def _field_kwargs(model, record, resolver, *, tenant_required: bool):
    valid_fields = {field.name: field for field in model._meta.concrete_fields}
    kwargs = {}
    unresolved = []
    unresolved_values = []
    missing_required = []
    for name, encoded in record.get("fields", {}).items():
        field = valid_fields.get(name)
        if (
            not field
            or field.primary_key
            or name.startswith("_")
            or name in SKIP_FIELD_NAMES
            or isinstance(field, models.FileField)
        ):
            continue
        if isinstance(field, (models.ForeignKey, models.OneToOneField)):
            continue
        value, available = _decode_archived_value(encoded, resolver)
        if available:
            kwargs[name] = value
        elif name in ("custom_field_data", "default"):
            kwargs[name] = value if value is not None else ({} if name == "custom_field_data" else None)
            unresolved_values.append((name, encoded))
        else:
            return None, [], [], []
    for name, spec in record.get("relations", {}).items():
        field = valid_fields.get(name)
        if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
            continue
        tenant_relation = is_tenant_relation(field)
        required_before_save = relation_required_before_save(field)
        value, available = resolver.resolve(spec)
        if available:
            if value is MISSING_REFERENCE:
                if (required_before_save and not tenant_relation) or (
                    not field.null and not field.has_default() and not tenant_relation
                ):
                    missing_required.append(name)
            elif value is None and tenant_relation and tenant_required:
                continue
            else:
                kwargs[name] = value
        elif field.null and not required_before_save:
            unresolved.append((name, spec))
        else:
            return None, [], [], []
    return kwargs, unresolved, unresolved_values, missing_required


def _set_generic_relations(obj, record, resolver, *, allow_deferred: bool):
    fields = {field.name: field for field in generic_foreign_keys(type(obj))}
    unresolved = []
    missing_required = []
    for name, spec in record.get("generic_relations", {}).items():
        field = fields.get(name)
        if not field:
            continue
        value, available = resolver.resolve(spec)
        if available:
            if value is MISSING_REFERENCE:
                ct_field = obj._meta.get_field(field.ct_field)
                id_field = obj._meta.get_field(field.fk_field)
                if not ct_field.null or not id_field.null:
                    missing_required.append(name)
            else:
                setattr(obj, name, value)
        elif allow_deferred:
            ct_field = obj._meta.get_field(field.ct_field)
            id_field = obj._meta.get_field(field.fk_field)
            if ct_field.null and id_field.null:
                unresolved.append((name, spec))
            else:
                return None
        else:
            return None
    return unresolved, missing_required


def _set_files(obj, record, assets, saved_files, *, dry_run: bool):
    if dry_run:
        return
    for name, spec in record.get("files", {}).items():
        if not spec or "path" not in spec or spec["path"] not in assets:
            continue
        filename = spec.get("name", spec["path"]).replace("\\", "/").rsplit("/", 1)[-1]
        file_value = getattr(obj, name)
        file_value.save(filename, ContentFile(assets[spec["path"]]), save=False)
        saved_files.append((file_value.storage, file_value.name))


def _cleanup_files(saved_files):
    for storage, name in reversed(saved_files):
        try:
            storage.delete(name)
        except Exception:
            logger.warning("Could not remove rolled-back import file %s", name, exc_info=True)


def _apply_m2m(obj, record, resolver):
    for name, specs in record.get("many_to_many", {}).items():
        try:
            manager = getattr(obj, name)
        except AttributeError:
            continue
        values = []
        for spec in specs:
            value, available = resolver.resolve(spec)
            if not available:
                raise ArchiveValidationError(f"M2M-Referenz für {record['id']} konnte nicht aufgelöst werden.")
            if value is MISSING_REFERENCE:
                continue
            values.append(value)
        manager.set(values)


def import_archive(parsed: ParsedArchive, *, conflict_strategy: str, dry_run: bool) -> ImportReport:
    if conflict_strategy not in ("update", "skip", "fail"):
        raise ArchiveValidationError("Unbekannte Konfliktstrategie.")
    source_version = str(parsed.manifest.get("source_netbox_version", ""))
    target_version = str(getattr(getattr(settings, "RELEASE", None), "version", ""))
    if source_version and target_version and source_version.split(".")[:2] != target_version.split(".")[:2]:
        raise ArchiveValidationError(
            f"NetBox-Versionen sind nicht kompatibel: Quelle {source_version}, Ziel {target_version}."
        )
    try:
        source_instance = uuid.UUID(parsed.manifest["source_instance"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveValidationError("Die Quellinstanz-Kennung fehlt oder ist ungültig.") from exc

    records = {}
    for record in parsed.records:
        if not all(key in record for key in ("id", "model", "source_pk")):
            raise ArchiveValidationError("Ein Objektdatensatz ist unvollständig.")
        if record["id"] in records:
            raise ArchiveValidationError(f"Doppelte Objekt-ID im Archiv: {record['id']}")
        records[record["id"]] = record

    report = ImportReport(dry_run=dry_run, warnings=list(parsed.warnings))
    resolved = {}
    resolver = ReferenceResolver(resolved, report.warnings, _model_for)
    compatibility = PluginCompatibility(report.warnings, dry_run=dry_run)
    deferred_relations = []
    deferred_generic = []
    deferred_values = []
    writable = set()
    saved_files = []

    try:
        with transaction.atomic():
            pending = dict(records)
            while pending:
                progressed = False
                for record_id, record in list(pending.items()):
                    model = _model_for(record["model"])
                    kwargs, unresolved, unresolved_value_fields, missing_required = _field_kwargs(
                        model, record, resolver, tenant_required=compatibility.tenant_required
                    )
                    if kwargs is None:
                        continue
                    try:
                        existing = _find_existing(source_instance, model, record, resolver)
                    except IdentityNotReady:
                        continue
                    if existing is not None and conflict_strategy == "fail":
                        raise ImportConflictError(f"Zielobjekt existiert bereits: {record_id}")

                    if existing is not None and conflict_strategy == "skip":
                        obj = existing
                        action = "skipped"
                    else:
                        obj = existing or model()
                        for name, value in kwargs.items():
                            setattr(obj, name, value)
                        generic_result = _set_generic_relations(obj, record, resolver, allow_deferred=True)
                        if generic_result is None:
                            continue
                        generic_unresolved, missing_generic = generic_result
                        missing_required.extend(missing_generic)
                        if missing_required and existing is None:
                            resolver.skip(record_id, record["model"], missing_required)
                            report.add(record["model"], "skipped")
                            pending.pop(record_id)
                            progressed = True
                            continue
                        _set_files(obj, record, parsed.assets, saved_files, dry_run=dry_run)
                        compatibility.save(obj)
                        action = "updated" if existing is not None else "created"
                        writable.add(record_id)
                        deferred_relations.extend((record_id, name, spec) for name, spec in unresolved)
                        deferred_generic.extend((record_id, name, spec) for name, spec in generic_unresolved)
                        deferred_values.extend(
                            (record_id, name, encoded) for name, encoded in unresolved_value_fields
                        )

                    resolved[record_id] = obj
                    _write_mapping(source_instance, record, obj)
                    report.add(record["model"], action)
                    report.mapped += 1
                    pending.pop(record_id)
                    progressed = True
                if not progressed:
                    blocked = ", ".join(list(pending)[:10])
                    raise ArchiveValidationError(
                        f"Erforderliche Referenzen konnten nicht aufgelöst werden: {blocked}"
                    )

            for record_id, name, spec in deferred_relations:
                if record_id not in writable:
                    continue
                value, available = resolver.resolve(spec)
                if not available:
                    raise ArchiveValidationError(f"Referenz {name} für {record_id} konnte nicht aufgelöst werden.")
                if value is MISSING_REFERENCE:
                    continue
                obj = resolved[record_id]
                setattr(obj, name, value)
                compatibility.save(obj, update_fields=[name])

            for record_id, name, spec in deferred_generic:
                if record_id not in writable:
                    continue
                value, available = resolver.resolve(spec)
                if not available:
                    raise ArchiveValidationError(f"Generische Referenz {name} für {record_id} fehlt.")
                if value is MISSING_REFERENCE:
                    continue
                obj = resolved[record_id]
                setattr(obj, name, value)
                field = next(field for field in generic_foreign_keys(type(obj)) if field.name == name)
                compatibility.save(obj, update_fields=[field.ct_field, field.fk_field])

            for record_id, name, encoded in deferred_values:
                if record_id not in writable:
                    continue
                value, available = _decode_archived_value(encoded, resolver)
                if not available:
                    raise ArchiveValidationError(f"Custom-Field-Referenz {name} für {record_id} fehlt.")
                obj = resolved[record_id]
                setattr(obj, name, value)
                compatibility.save(obj, update_fields=[name])

            for record_id, record in records.items():
                if record_id in writable:
                    _apply_m2m(resolved[record_id], record, resolver)

            if dry_run:
                transaction.set_rollback(True)
    except (IntegrityError, ValueError, TypeError) as exc:
        _cleanup_files(saved_files)
        raise ArchiveValidationError(f"Der Import wurde zurückgerollt: {exc}") from exc
    except ExportImportError:
        _cleanup_files(saved_files)
        raise
    except Exception as exc:
        _cleanup_files(saved_files)
        raise ArchiveValidationError(f"Der Import wurde zurückgerollt: {exc}") from exc
    return report
