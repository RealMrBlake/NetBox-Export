from __future__ import annotations

import io
import json
import logging
import math
import re
import threading
import uuid
import warnings
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import IntegrityError, models, transaction
from PIL import Image as PillowImage

from netbox_export.models import ImportedObjectMapping

from .archive import ParsedArchive
from .codec import SKIP_FIELD_NAMES, decode_scalar, generic_foreign_keys, is_range_value
from .exceptions import ArchiveValidationError, ExportImportError, ImportConflictError
from .plugin_compat import (
    PluginCompatibility,
    is_tenant_relation,
    relation_required_before_save,
    relation_should_be_deferred,
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
    "dcim.frontport": ("device", "name"),
    "dcim.rearport": ("device", "name"),
    "dcim.portmapping": ("front_port", "front_port_position"),
    "dcim.devicebay": ("device", "name"),
    "dcim.modulebay": ("device", "module", "name"),
    "dcim.inventoryitem": ("device", "parent", "name"),
    "ipam.prefix": ("vrf", "prefix"),
    "ipam.ipaddress": ("vrf", "address"),
    "ipam.vlan": ("group", "vid"),
    "circuits.circuit": ("provider", "cid"),
    "virtualization.virtualmachine": ("cluster", "tenant", "name"),
    "virtualization.vminterface": ("virtual_machine", "name"),
}

NETBOX_IMAGE_MAX_PIXELS = 25_000_000
IMPORTED_IMAGE_TARGET_PIXELS = 20_000_000
IMPORTED_IMAGE_SOURCE_MAX_PIXELS = 100_000_000
_IMAGE_LIMIT_LOCK = threading.Lock()
_LEGACY_RANGE_PATTERN = re.compile(r"^([\[(])(.*),(.*)([\])])$")


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


@dataclass(frozen=True)
class DeferredDevicePlacement:
    rack_spec: dict | None
    position: object
    face: object


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


def _range_endpoint(field, value):
    value = value.strip()
    if not value:
        return None
    if value.startswith('"') and value.endswith('"'):
        value = json.loads(value)
    return field.base_field.to_python(value)


def _coerce_range_value(field, value):
    if value is None or is_range_value(value):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return field.range_type(value[0], value[1])
    if value == "empty":
        return field.range_type(empty=True)
    if not isinstance(value, str) or not (match := _LEGACY_RANGE_PATTERN.fullmatch(value.strip())):
        return value
    bounds = match.group(1) + match.group(4)
    lower = _range_endpoint(field, match.group(2))
    upper = _range_endpoint(field, match.group(3))
    return field.range_type(lower, upper, bounds=bounds)


def _coerce_model_field_value(field, value):
    if hasattr(field, "range_type") and hasattr(field, "base_field"):
        return _coerce_range_value(field, value)
    base_field = getattr(field, "base_field", None)
    if (
        isinstance(value, (list, tuple))
        and base_field is not None
        and hasattr(base_field, "range_type")
        and hasattr(base_field, "base_field")
    ):
        return [_coerce_range_value(base_field, item) for item in value]
    return value


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
    lookup = _identity_lookup(model, record, resolver)
    if not lookup:
        return mapped
    try:
        natural = model._default_manager.get(**lookup)
    except model.DoesNotExist:
        return mapped
    except model.MultipleObjectsReturned as exc:
        raise ImportConflictError(
            f"Mehrere Zielobjekte passen auf {record['model']} mit {lookup}."
        ) from exc
    if mapped is not None and mapped.pk != natural.pk:
        resolver.warn(
            ("mapping-rebound", record["id"]),
            f"Gespeicherte Zuordnung für {record['id']} wurde von Ziel-ID {mapped.pk} auf "
            f"Ziel-ID {natural.pk} korrigiert, da der Fachschlüssel {lookup} bereits existiert.",
        )
    return natural


def _defer_device_placement(model, record):
    if model._meta.label_lower != "dcim.device":
        return record, None

    fields = record.get("fields", {})
    relations = record.get("relations", {})
    if "rack" not in relations or "position" not in fields or "face" not in fields:
        return record, None

    prepared = dict(record)
    prepared["fields"] = {
        name: value for name, value in fields.items() if name not in ("position", "face")
    }
    prepared["relations"] = {name: value for name, value in relations.items() if name != "rack"}
    placement = DeferredDevicePlacement(
        rack_spec=relations["rack"],
        position=decode_scalar(fields["position"]),
        face=decode_scalar(fields["face"]),
    )
    return prepared, placement


def _placement_can_be_applied(placement, resolver):
    rack, available = resolver.resolve(placement.rack_spec)
    return not (available and rack is MISSING_REFERENCE)


def _stage_device_placement(device):
    device.position = None
    device.face = None


def _device_footprint(device):
    device_type = getattr(device, "device_type", None)
    height = Decimal(str(getattr(device_type, "u_height", 1) or 0))
    if height <= 0:
        height = Decimal("0.5")
    return height, bool(getattr(device_type, "is_full_depth", False))


def _device_placement_conflicts(device, rack, position, face):
    if rack is None or position is None:
        return []

    position = Decimal(str(position))
    height, full_depth = _device_footprint(device)
    end = position + height
    candidates = (
        type(device)
        ._default_manager.select_for_update()
        .select_related("device_type")
        .filter(rack=rack, position__isnull=False)
        .exclude(pk=device.pk)
    )
    conflicts = []
    for candidate in candidates:
        candidate_position = Decimal(str(candidate.position))
        candidate_height, candidate_full_depth = _device_footprint(candidate)
        faces_overlap = full_depth or candidate_full_depth or candidate.face == face
        positions_overlap = position < candidate_position + candidate_height and candidate_position < end
        if faces_overlap and positions_overlap:
            conflicts.append(candidate)
    return conflicts


def _placement_target(rack, position, face):
    rack_name = getattr(rack, "name", None) or str(rack.pk)
    return f"Rack {rack_name}, Position {position}, Seite {face or '-'}"


def _conflicting_devices(conflicts):
    return ", ".join(
        f"{device.pk} ({getattr(device, 'name', None) or 'ohne Namen'})" for device in conflicts
    )


def _save_device_placement(device, rack, position, face, compatibility):
    device.rack = rack
    device.position = position
    device.face = face
    update_fields = ["rack", "position", "face"]
    rack_location = getattr(rack, "location", None)
    if rack_location is not None:
        device.location = rack_location
        update_fields.append("location")
    compatibility.save(device, update_fields=update_fields)


def _apply_device_placements(
    placements,
    resolved,
    resolver,
    compatibility,
    *,
    conflict_strategy,
):
    for record_id, placement in placements:
        rack, available = resolver.resolve(placement.rack_spec)
        if not available:
            raise ArchiveValidationError(f"Rack-Referenz für {record_id} konnte nicht aufgelöst werden.")
        if rack is MISSING_REFERENCE:
            continue

        device = resolved[record_id]
        position = placement.position
        face = placement.face
        if rack is None and (position is not None or face is not None):
            resolver.warn(
                ("device-placement-without-rack", record_id),
                f"Rackplatz für {record_id} wurde ausgelassen, da kein Rack zugeordnet ist.",
            )
            position = None
            face = None

        if rack is not None and position is not None:
            rack = type(rack)._default_manager.select_for_update().get(pk=rack.pk)

        conflicts = _device_placement_conflicts(device, rack, position, face)
        if not conflicts:
            _save_device_placement(device, rack, position, face, compatibility)
            continue

        target = _placement_target(rack, position, face)
        occupants = _conflicting_devices(conflicts)
        if conflict_strategy == "fail":
            raise ImportConflictError(
                f"Rackplatzkonflikt für {record_id}: {target} ist durch Zielgerät(e) {occupants} belegt."
            )
        if conflict_strategy == "skip":
            _save_device_placement(device, rack, None, None, compatibility)
            resolver.warn(
                ("device-placement-skipped", record_id),
                f"Rackplatz für {record_id} wurde übersprungen: {target} bleibt durch "
                f"Zielgerät(e) {occupants} belegt. Das importierte Gerät wurde ohne Position im Rack gespeichert.",
            )
            continue

        for conflict in conflicts:
            conflict.position = None
            conflict.face = None
            compatibility.save(conflict, update_fields=["position", "face"])
        _save_device_placement(device, rack, position, face, compatibility)
        resolver.warn(
            ("device-placement-released", record_id),
            f"Rackplatzkonflikt für {record_id} gelöst: Zielgerät(e) {occupants} wurden aus {target} "
            "gelöst und ohne Position im Rack belassen.",
        )


def _write_mapping(source_instance, record, obj):
    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    ImportedObjectMapping.objects.update_or_create(
        source_instance=source_instance,
        source_model=record["model"],
        source_object_id=record["source_pk"],
        defaults={"target_type": content_type, "target_id": str(obj.pk)},
    )


def _release_port_mapping_conflicts(obj, resolver, record_id):
    if obj._meta.label_lower != "dcim.portmapping":
        return

    conflicts = type(obj)._default_manager.select_for_update().filter(
        models.Q(
            front_port_id=obj.front_port_id,
            front_port_position=obj.front_port_position,
        )
        | models.Q(
            rear_port_id=obj.rear_port_id,
            rear_port_position=obj.rear_port_position,
        )
    )
    if obj.pk is not None:
        conflicts = conflicts.exclude(pk=obj.pk)
    conflicts = list(conflicts)
    if not conflicts:
        return

    conflict_ids = ", ".join(str(conflict.pk) for conflict in conflicts)
    for conflict in conflicts:
        conflict.delete()
    resolver.warn(
        ("port-mapping-replaced", record_id),
        f"Portzuordnung(en) {conflict_ids} wurden durch {record_id} ersetzt, damit die "
        "Front-/Rear-Port-Verkabelung der Quelle entspricht.",
    )


def _trace_paths_signal():
    from dcim.models.cables import trace_paths

    return trace_paths


def _rebuild_imported_cable_paths(records, resolved):
    cable_ids = {
        resolved[record_id].cable_id
        for record_id, record in records.items()
        if record["model"] == "dcim.cabletermination" and record_id in resolved
    }
    if not cable_ids:
        return

    cable_model = _model_for("dcim.cable")
    trace_paths = _trace_paths_signal()
    for cable in cable_model._default_manager.filter(pk__in=cable_ids).order_by("pk"):
        # CableTermination records are imported directly, so Cable.save() never
        # emits NetBox's normal path-rebuild signal for their new endpoints.
        cable._terminations_modified = True
        trace_paths.send(cable_model, instance=cable, created=False)


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
            kwargs[name] = _coerce_model_field_value(field, value)
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
            elif value is not None and relation_should_be_deferred(field):
                unresolved.append((name, spec))
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


def _open_image_with_bounded_override(data):
    stream = io.BytesIO(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            image = PillowImage.open(stream)
        return stream, image
    except (PillowImage.DecompressionBombError, PillowImage.DecompressionBombWarning):
        stream.close()
    except Exception:
        stream.close()
        raise

    stream = io.BytesIO(data)
    with _IMAGE_LIMIT_LOCK:
        original_limit = PillowImage.MAX_IMAGE_PIXELS
        PillowImage.MAX_IMAGE_PIXELS = IMPORTED_IMAGE_SOURCE_MAX_PIXELS
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PillowImage.DecompressionBombWarning)
                image = PillowImage.open(stream)
        except PillowImage.DecompressionBombError as exc:
            stream.close()
            raise ArchiveValidationError(
                "Das Bild überschreitet die sichere Importgrenze von "
                f"{IMPORTED_IMAGE_SOURCE_MAX_PIXELS:,} Pixeln."
            ) from exc
        except Exception:
            stream.close()
            raise
        finally:
            PillowImage.MAX_IMAGE_PIXELS = original_limit
    return stream, image


def _resized_image_save_options(image_format, image):
    options = {}
    if icc_profile := image.info.get("icc_profile"):
        options["icc_profile"] = icc_profile
    if image_format in ("JPEG", "MPO"):
        options.update(quality=85, optimize=True, progressive=True)
    elif image_format == "WEBP":
        options.update(quality=85, method=4)
    elif image_format in ("PNG", "GIF"):
        options["optimize"] = True
    elif image_format == "TIFF":
        options["compression"] = "tiff_deflate"
    return options


def _prepare_image_asset(data):
    stream, image = _open_image_with_bounded_override(data)
    try:
        width, height = image.size
        source_pixels = width * height
        if source_pixels > IMPORTED_IMAGE_SOURCE_MAX_PIXELS:
            raise ArchiveValidationError(
                f"Das Bild mit {source_pixels:,} Pixeln überschreitet die sichere Importgrenze von "
                f"{IMPORTED_IMAGE_SOURCE_MAX_PIXELS:,} Pixeln."
            )
        if source_pixels <= NETBOX_IMAGE_MAX_PIXELS:
            return data, width, height, None

        scale = math.sqrt(IMPORTED_IMAGE_TARGET_PIXELS / source_pixels)
        target_size = (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
        image.thumbnail(target_size, PillowImage.Resampling.LANCZOS, reducing_gap=3.0)
        image_format = image.format or "PNG"
        if image_format == "MPO":
            image_format = "JPEG"
        output = io.BytesIO()
        image.save(output, format=image_format, **_resized_image_save_options(image_format, image))
        resized_width, resized_height = image.size
        resize_info = (width, height, resized_width, resized_height)
        return output.getvalue(), resized_width, resized_height, resize_info
    finally:
        image.close()
        stream.close()


def _set_files(obj, record, assets, saved_files, *, dry_run: bool, import_warnings=None):
    for name, spec in record.get("files", {}).items():
        if not spec or "path" not in spec or spec["path"] not in assets:
            continue
        filename = spec.get("name", spec["path"]).replace("\\", "/").rsplit("/", 1)[-1]
        field = obj._meta.get_field(name)
        content_data = assets[spec["path"]]
        dimensions = (None, None)
        if isinstance(field, models.ImageField):
            content_data, width, height, resize_info = _prepare_image_asset(content_data)
            dimensions = (width, height)
            if field.width_field and width is not None:
                setattr(obj, field.width_field, width)
            if field.height_field and height is not None:
                setattr(obj, field.height_field, height)
            if resize_info is not None and import_warnings is not None:
                old_width, old_height, new_width, new_height = resize_info
                record_id = record.get("id", obj._meta.label_lower)
                import_warnings.append(
                    f"Bild für {record_id} wurde von {old_width}×{old_height} auf "
                    f"{new_width}×{new_height} Pixel verkleinert."
                )
        if dry_run:
            continue
        content = ContentFile(content_data)
        file_value = getattr(obj, name)
        file_value.save(filename, content, save=False)
        saved_files.append((file_value.storage, file_value.name))
        width, height = dimensions
        if isinstance(field, models.ImageField) and field.width_field and width is not None:
            setattr(obj, field.width_field, width)
        if isinstance(field, models.ImageField) and field.height_field and height is not None:
            setattr(obj, field.height_field, height)


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
    deferred_device_placements = []
    writable = set()
    saved_files = []

    try:
        with transaction.atomic():
            pending = dict(records)
            while pending:
                progressed = False
                for record_id, record in list(pending.items()):
                    model = _model_for(record["model"])
                    prepared_record, device_placement = _defer_device_placement(model, record)
                    if device_placement is not None and not _placement_can_be_applied(
                        device_placement, resolver
                    ):
                        device_placement = None
                    kwargs, unresolved, unresolved_value_fields, missing_required = _field_kwargs(
                        model,
                        prepared_record,
                        resolver,
                        tenant_required=compatibility.tenant_required,
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
                        _set_files(
                            obj,
                            record,
                            parsed.assets,
                            saved_files,
                            dry_run=dry_run,
                            import_warnings=report.warnings,
                        )
                        if device_placement is not None:
                            _stage_device_placement(obj)
                        compatibility.prepare_initial_save(obj, is_new=existing is None)
                        _release_port_mapping_conflicts(obj, resolver, record_id)
                        compatibility.save(obj)
                        action = "updated" if existing is not None else "created"
                        writable.add(record_id)
                        deferred_relations.extend((record_id, name, spec) for name, spec in unresolved)
                        deferred_generic.extend((record_id, name, spec) for name, spec in generic_unresolved)
                        deferred_values.extend(
                            (record_id, name, encoded) for name, encoded in unresolved_value_fields
                        )
                        if device_placement is not None:
                            deferred_device_placements.append((record_id, device_placement))

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
                compatibility.release_unique_relation(obj, name, value)
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

            _apply_device_placements(
                deferred_device_placements,
                resolved,
                resolver,
                compatibility,
                conflict_strategy=conflict_strategy,
            )

            for record_id, record in records.items():
                if record_id in writable:
                    _apply_m2m(resolved[record_id], record, resolver)

            _rebuild_imported_cable_paths(records, resolved)

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
