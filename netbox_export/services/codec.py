from __future__ import annotations

import base64
import datetime
import decimal
import uuid
from pathlib import PurePosixPath

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models

SKIP_FIELD_NAMES = {
    "id",
    "created",
    "last_updated",
    "lft",
    "rght",
    "tree_id",
    "level",
}


def object_key(obj) -> str:
    return f"{obj._meta.label_lower}:{obj.pk}"


def model_label(model) -> str:
    return model._meta.label_lower


def encode_scalar(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, uuid.UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, datetime.datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"$type": "duration", "value": value.total_seconds()}
    if isinstance(value, bytes):
        return {"$type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [encode_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode_scalar(item) for key, item in value.items()}
    return str(value)


def decode_scalar(value):
    if isinstance(value, list):
        return [decode_scalar(item) for item in value]
    if not isinstance(value, dict) or "$type" not in value:
        if isinstance(value, dict):
            return {key: decode_scalar(item) for key, item in value.items()}
        return value
    kind = value["$type"]
    raw = value.get("value")
    decoders = {
        "decimal": decimal.Decimal,
        "uuid": uuid.UUID,
        "datetime": datetime.datetime.fromisoformat,
        "date": datetime.date.fromisoformat,
        "time": datetime.time.fromisoformat,
        "duration": lambda item: datetime.timedelta(seconds=item),
        "bytes": lambda item: base64.b64decode(item.encode("ascii")),
    }
    return decoders[kind](raw)


def generic_foreign_keys(model) -> list[GenericForeignKey]:
    return [field for field in model._meta.private_fields if isinstance(field, GenericForeignKey)]


def external_identity(obj) -> dict:
    label = obj._meta.label_lower
    if label in ("contenttypes.contenttype", "core.objecttype"):
        return {"model": label, "lookup": {"app_label": obj.app_label, "model": obj.model}}
    for field_name in ("username", "slug", "name"):
        if hasattr(obj, field_name):
            return {"model": label, "lookup": {field_name: encode_scalar(getattr(obj, field_name))}}
    return {"model": label, "lookup": {"pk": encode_scalar(obj.pk)}}


def _reference_spec(obj, exported_keys: set[str]):
    if object_key(obj) in exported_keys:
        return {"ref": object_key(obj)}
    return {"external": external_identity(obj)}


def _encode_custom_field_data(obj, value: dict, exported_keys: set[str]):
    from extras.models import CustomField

    definitions = {field.name: field for field in CustomField.objects.get_for_model(type(obj))}
    encoded = {}
    for name, raw_value in value.items():
        custom_field = definitions.get(name)
        if (
            custom_field is None
            or custom_field.type not in ("object", "multiobject")
            or not custom_field.related_object_type
            or raw_value in (None, "", [])
        ):
            encoded[name] = encode_scalar(raw_value)
            continue
        target_model = custom_field.related_object_type.model_class()
        if custom_field.type == "object":
            try:
                target = target_model._default_manager.get(pk=raw_value)
            except target_model.DoesNotExist:
                encoded[name] = None
            else:
                encoded[name] = {"$type": "object_ref", "value": _reference_spec(target, exported_keys)}
        else:
            targets = {str(item.pk): item for item in target_model._default_manager.filter(pk__in=raw_value)}
            encoded[name] = {
                "$type": "multiobject_ref",
                "value": [
                    _reference_spec(targets[str(pk)], exported_keys)
                    for pk in raw_value
                    if str(pk) in targets
                ],
            }
    return encoded


def _encode_custom_field_default(custom_field, value, exported_keys: set[str]):
    if (
        custom_field.type not in ("object", "multiobject")
        or not custom_field.related_object_type
        or value in (None, "", [])
    ):
        return encode_scalar(value)
    target_model = custom_field.related_object_type.model_class()
    if custom_field.type == "object":
        try:
            target = target_model._default_manager.get(pk=value)
        except target_model.DoesNotExist:
            return None
        return {"$type": "object_ref", "value": _reference_spec(target, exported_keys)}
    targets = {str(item.pk): item for item in target_model._default_manager.filter(pk__in=value)}
    return {
        "$type": "multiobject_ref",
        "value": [
            _reference_spec(targets[str(pk)], exported_keys)
            for pk in value
            if str(pk) in targets
        ],
    }


def serialize_object(obj, exported_keys: set[str], assets: dict[str, bytes]) -> dict:
    record_id = object_key(obj)
    gfk_fields = generic_foreign_keys(type(obj))
    gfk_storage = {name for field in gfk_fields for name in (field.ct_field, field.fk_field)}
    scalars = {}
    relations = {}
    files = {}

    for field in obj._meta.concrete_fields:
        if field.primary_key or field.name.startswith("_") or field.name in SKIP_FIELD_NAMES or field.name in gfk_storage:
            continue
        if isinstance(field, (models.ForeignKey, models.OneToOneField)):
            related = getattr(obj, field.name, None)
            if related is None:
                relations[field.name] = None
            elif object_key(related) in exported_keys:
                relations[field.name] = {"ref": object_key(related)}
            else:
                relations[field.name] = {"external": external_identity(related)}
            continue
        if isinstance(field, models.FileField):
            file_value = getattr(obj, field.name)
            if not file_value:
                files[field.name] = None
                continue
            filename = PurePosixPath(str(file_value.name)).name
            archive_path = f"assets/{record_id.replace(':', '_')}/{field.name}/{filename}"
            try:
                with file_value.open("rb") as source:
                    assets[archive_path] = source.read()
            except (FileNotFoundError, OSError, ValueError):
                files[field.name] = {"missing": str(file_value.name)}
            else:
                files[field.name] = {"path": archive_path, "name": str(file_value.name)}
            continue
        value = field.value_from_object(obj)
        if field.name == "custom_field_data" and isinstance(value, dict):
            scalars[field.name] = _encode_custom_field_data(obj, value, exported_keys)
        elif obj._meta.label_lower == "extras.customfield" and field.name == "default":
            scalars[field.name] = _encode_custom_field_default(obj, value, exported_keys)
        else:
            scalars[field.name] = encode_scalar(value)

    generic_relations = {}
    for field in gfk_fields:
        related = getattr(obj, field.name, None)
        if related is None:
            generic_relations[field.name] = None
        elif object_key(related) in exported_keys:
            generic_relations[field.name] = {"ref": object_key(related)}
        else:
            generic_relations[field.name] = {"external": external_identity(related)}

    many_to_many = {}
    for field in obj._meta.many_to_many:
        try:
            values = list(getattr(obj, field.name).all())
        except (AttributeError, TypeError):
            continue
        many_to_many[field.name] = [
            {"ref": object_key(item)} if object_key(item) in exported_keys else {"external": external_identity(item)}
            for item in values
        ]

    return {
        "id": record_id,
        "model": obj._meta.label_lower,
        "source_pk": str(obj.pk),
        "fields": scalars,
        "relations": relations,
        "generic_relations": generic_relations,
        "many_to_many": many_to_many,
        "files": files,
    }
