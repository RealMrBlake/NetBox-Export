from __future__ import annotations

import hashlib
import hmac
import io
import json
import posixpath
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO

from django.core.serializers.json import DjangoJSONEncoder

from .exceptions import ArchiveValidationError

FORMAT_NAME = "netbox-export"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
OBJECTS_NAME = "objects.ndjson"


def _json_bytes(value) -> bytes:
    return json.dumps(
        value,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _signature(manifest: dict, signing_key: str) -> str:
    signed_manifest = {key: value for key, value in manifest.items() if key != "signature"}
    return hmac.new(signing_key.encode("utf-8"), _json_bytes(signed_manifest), hashlib.sha256).hexdigest()


def build_archive(manifest: dict, records: Iterable[dict], assets: dict[str, bytes], signing_key: str = "") -> bytes:
    normalized_assets = {}
    for path, content in assets.items():
        safe_path = _safe_member_name(path)
        if safe_path in normalized_assets:
            raise ArchiveValidationError(f"Doppelter Dateipfad im Archiv: {safe_path}")
        normalized_assets[safe_path] = content
    assets = normalized_assets
    object_data = b"\n".join(_json_bytes(record) for record in records) + b"\n"
    checksum = hashlib.sha256(object_data).hexdigest()
    manifest = {
        **manifest,
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "objects_sha256": checksum,
        "assets_sha256": {
            path: hashlib.sha256(content).hexdigest()
            for path, content in sorted(assets.items())
        },
    }
    if signing_key:
        manifest["signature"] = _signature(manifest, signing_key)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(MANIFEST_NAME, _json_bytes(manifest))
        archive.writestr(OBJECTS_NAME, object_data)
        for path, content in sorted(assets.items()):
            archive.writestr(_safe_member_name(path), content)
    return output.getvalue()


def _safe_member_name(name: str) -> str:
    normalized = posixpath.normpath(name.replace("\\", "/"))
    if normalized.startswith(("../", "/")) or normalized in ("", ".", ".."):
        raise ArchiveValidationError(f"Unsicherer Dateipfad im Archiv: {name}")
    return normalized


@dataclass(frozen=True)
class ParsedArchive:
    manifest: dict
    records: list[dict]
    assets: dict[str, bytes]
    warnings: list[str]


def read_archive(
    source: bytes | BinaryIO,
    *,
    max_size: int,
    max_objects: int,
    signing_key: str = "",
) -> ParsedArchive:
    if isinstance(source, bytes):
        raw = source
    else:
        raw = source.read(max_size + 1)
    if len(raw) > max_size:
        raise ArchiveValidationError("Das Archiv überschreitet die konfigurierte Maximalgröße.")

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ArchiveValidationError("Die hochgeladene Datei ist kein gültiges ZIP-Archiv.") from exc

    with archive:
        names = {}
        for info in archive.infolist():
            name = _safe_member_name(info.filename)
            if name in names:
                raise ArchiveValidationError(f"Doppelter Dateipfad im Archiv: {name}")
            names[name] = info
        if len(names) > max_objects * 5 + 2:
            raise ArchiveValidationError("Das Archiv enthält zu viele Dateien.")
        if MANIFEST_NAME not in names or OBJECTS_NAME not in names:
            raise ArchiveValidationError("manifest.json oder objects.ndjson fehlt im Archiv.")
        total_size = sum(info.file_size for info in names.values())
        if total_size > max_size * 4:
            raise ArchiveValidationError("Der entpackte Archivinhalt ist zu groß.")
        if any(info.file_size > 0 and info.compress_size * 200 < info.file_size for info in names.values()):
            raise ArchiveValidationError("Das Archiv weist ein unzulässiges Kompressionsverhältnis auf.")

        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
            object_data = archive.read(OBJECTS_NAME)
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise ArchiveValidationError("Die Metadaten im Archiv sind ungültig.") from exc

        if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") != FORMAT_VERSION:
            raise ArchiveValidationError("Das Archivformat oder dessen Version wird nicht unterstützt.")
        checksum = hashlib.sha256(object_data).hexdigest()
        if not hmac.compare_digest(checksum, str(manifest.get("objects_sha256", ""))):
            raise ArchiveValidationError("Die Objektdatei stimmt nicht mit ihrer Prüfsumme überein.")

        warnings = []
        signature = manifest.get("signature")
        if signing_key:
            expected = _signature(manifest, signing_key)
            if not signature or not hmac.compare_digest(expected, signature):
                raise ArchiveValidationError("Die Archivsignatur ist ungültig oder fehlt.")
        elif signature:
            warnings.append("Das Archiv ist signiert, aber ohne konfigurierten Schlüssel nicht verifiziert.")
        else:
            warnings.append("Das Archiv ist nicht signiert.")

        records = []
        try:
            for line in object_data.splitlines():
                if line:
                    records.append(json.loads(line))
                    if len(records) > max_objects:
                        raise ArchiveValidationError("Das Archiv enthält zu viele Objekte.")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveValidationError("objects.ndjson enthält ungültiges JSON.") from exc

        assets = {
            name: archive.read(name)
            for name in names
            if name not in (MANIFEST_NAME, OBJECTS_NAME)
        }
        declared_assets = manifest.get("assets_sha256", {})
        if set(declared_assets) != set(assets):
            raise ArchiveValidationError("Die Dateiliste stimmt nicht mit dem Manifest überein.")
        for name, content in assets.items():
            actual = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual, str(declared_assets.get(name, ""))):
                raise ArchiveValidationError(f"Die Prüfsumme der Datei {name} ist ungültig.")
        if manifest.get("object_count") != len(records):
            raise ArchiveValidationError("Die Objektanzahl stimmt nicht mit dem Manifest überein.")
        return ParsedArchive(manifest=manifest, records=records, assets=assets, warnings=warnings)
