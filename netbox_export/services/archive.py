from __future__ import annotations

import hashlib
import hmac
import io
import json
import posixpath
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.translation import gettext as _

from .exceptions import ArchiveValidationError

FORMAT_NAME = "netbox-export"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
OBJECTS_NAME = "objects.ndjson"
PRECOMPRESSED_SUFFIXES = {
    ".7z",
    ".avi",
    ".gz",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".webp",
    ".zip",
}


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
            raise ArchiveValidationError(_("Duplicate file path in archive: %(path)s") % {"path": safe_path})
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
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr(MANIFEST_NAME, _json_bytes(manifest))
        archive.writestr(OBJECTS_NAME, object_data)
        for path, content in sorted(assets.items()):
            compression = (
                zipfile.ZIP_STORED
                if PurePosixPath(path).suffix.lower() in PRECOMPRESSED_SUFFIXES
                else zipfile.ZIP_DEFLATED
            )
            archive.writestr(
                _safe_member_name(path),
                content,
                compress_type=compression,
                compresslevel=None if compression == zipfile.ZIP_STORED else 1,
            )
    return output.getvalue()


def _safe_member_name(name: str) -> str:
    normalized = posixpath.normpath(name.replace("\\", "/"))
    if normalized.startswith(("../", "/")) or normalized in ("", ".", ".."):
        raise ArchiveValidationError(_("Unsafe file path in archive: %(path)s") % {"path": name})
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
        raise ArchiveValidationError(_("The archive exceeds the configured maximum size."))

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ArchiveValidationError(_("The uploaded file is not a valid ZIP archive.")) from exc

    with archive:
        names = {}
        for info in archive.infolist():
            name = _safe_member_name(info.filename)
            if name in names:
                raise ArchiveValidationError(_("Duplicate file path in archive: %(path)s") % {"path": name})
            names[name] = info
        if len(names) > max_objects * 5 + 2:
            raise ArchiveValidationError(_("The archive contains too many files."))
        if MANIFEST_NAME not in names or OBJECTS_NAME not in names:
            raise ArchiveValidationError(_("manifest.json or objects.ndjson is missing from the archive."))
        total_size = sum(info.file_size for info in names.values())
        if total_size > max_size * 4:
            raise ArchiveValidationError(_("The extracted archive content is too large."))
        if any(info.file_size > 0 and info.compress_size * 200 < info.file_size for info in names.values()):
            raise ArchiveValidationError(_("The archive has an impermissible compression ratio."))

        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
            object_data = archive.read(OBJECTS_NAME)
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise ArchiveValidationError(_("The archive metadata is invalid.")) from exc

        if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") != FORMAT_VERSION:
            raise ArchiveValidationError(_("The archive format or its version is not supported."))
        checksum = hashlib.sha256(object_data).hexdigest()
        if not hmac.compare_digest(checksum, str(manifest.get("objects_sha256", ""))):
            raise ArchiveValidationError(_("The object file does not match its checksum."))

        warnings = []
        signature = manifest.get("signature")
        if signing_key:
            expected = _signature(manifest, signing_key)
            if not signature or not hmac.compare_digest(expected, signature):
                raise ArchiveValidationError(_("The archive signature is invalid or missing."))
        elif signature:
            warnings.append(_("The archive is signed but was not verified because no key is configured."))
        else:
            warnings.append(_("The archive is not signed."))

        records = []
        try:
            for line in object_data.splitlines():
                if line:
                    records.append(json.loads(line))
                    if len(records) > max_objects:
                        raise ArchiveValidationError(_("The archive contains too many objects."))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveValidationError(_("objects.ndjson contains invalid JSON.")) from exc

        assets = {
            name: archive.read(name)
            for name in names
            if name not in (MANIFEST_NAME, OBJECTS_NAME)
        }
        declared_assets = manifest.get("assets_sha256", {})
        if set(declared_assets) != set(assets):
            raise ArchiveValidationError(_("The file list does not match the manifest."))
        for name, content in assets.items():
            actual = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual, str(declared_assets.get(name, ""))):
                raise ArchiveValidationError(_("The checksum of file %(name)s is invalid.") % {"name": name})
        if manifest.get("object_count") != len(records):
            raise ArchiveValidationError(_("The object count does not match the manifest."))
        return ParsedArchive(manifest=manifest, records=records, assets=assets, warnings=warnings)
