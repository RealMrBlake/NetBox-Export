import io
import json
import zipfile

import pytest

from netbox_export.services.archive import build_archive, read_archive
from netbox_export.services.exceptions import ArchiveValidationError


def manifest():
    return {
        "source_instance": "b89196f8-3d87-466a-9278-f68e22d6d2cc",
        "scope": {"type": "site", "source_pk": "1", "label": "Berlin"},
        "object_count": 1,
    }


def records():
    return [
        {
            "id": "dcim.site:1",
            "model": "dcim.site",
            "source_pk": "1",
            "fields": {"name": "Berlin"},
        }
    ]


def test_archive_round_trip():
    payload = build_archive(manifest(), records(), {"assets/a.txt": b"data"})

    parsed = read_archive(payload, max_size=1024 * 1024, max_objects=10)

    assert parsed.records == records()
    assert parsed.assets == {"assets/a.txt": b"data"}
    assert parsed.warnings == ["Das Archiv ist nicht signiert."]


def test_signed_archive_requires_matching_key():
    payload = build_archive(manifest(), records(), {}, signing_key="secret-a")

    with pytest.raises(ArchiveValidationError, match="Archivsignatur"):
        read_archive(payload, max_size=1024 * 1024, max_objects=10, signing_key="secret-b")


def test_signed_archive_without_local_key_is_not_claimed_as_verified():
    payload = build_archive(manifest(), records(), {}, signing_key="secret-a")

    parsed = read_archive(payload, max_size=1024 * 1024, max_objects=10)

    assert parsed.warnings == ["Das Archiv ist signiert, aber ohne konfigurierten Schlüssel nicht verifiziert."]


def test_modified_object_stream_is_rejected():
    payload = build_archive(manifest(), records(), {})
    source = zipfile.ZipFile(io.BytesIO(payload))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "objects.ndjson":
                content = content.replace(b"Berlin", b"Hamburg")
            target.writestr(info, content)

    with pytest.raises(ArchiveValidationError, match="Prüfsumme"):
        read_archive(output.getvalue(), max_size=1024 * 1024, max_objects=10)


def test_modified_asset_is_rejected():
    payload = build_archive(manifest(), records(), {"assets/a.txt": b"original"})
    source = zipfile.ZipFile(io.BytesIO(payload))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "assets/a.txt":
                content = b"modified"
            target.writestr(info, content)

    with pytest.raises(ArchiveValidationError, match="Prüfsumme der Datei"):
        read_archive(output.getvalue(), max_size=1024 * 1024, max_objects=10)



def test_path_traversal_is_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../manifest.json", json.dumps(manifest()))

    with pytest.raises(ArchiveValidationError, match="Dateipfad"):
        read_archive(output.getvalue(), max_size=1024 * 1024, max_objects=10)


def test_object_limit_is_enforced():
    payload = build_archive(manifest(), records() * 2, {})

    with pytest.raises(ArchiveValidationError, match="zu viele Objekte"):
        read_archive(payload, max_size=1024 * 1024, max_objects=1)
