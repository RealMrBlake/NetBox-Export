import uuid

import pytest
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models

from netbox_export.services.archive import ParsedArchive
from netbox_export.services.exceptions import ArchiveValidationError
from netbox_export.services import importer as importer_module
from netbox_export.services.importer import import_archive

pytestmark = pytest.mark.django_db(transaction=True)


class FragileObject(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "force_tests"

    def save(self, *args, **kwargs):
        if self.name == "bad":
            raise ValueError("kaputt")
        return super().save(*args, **kwargs)


class Assignment(models.Model):
    assigned_object_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    assigned_object_id = models.PositiveBigIntegerField()
    assigned_object = GenericForeignKey("assigned_object_type", "assigned_object_id")

    class Meta:
        app_label = "force_tests"


@pytest.fixture
def force_tables(monkeypatch):
    test_models = {"force_tests.fragileobject": FragileObject, "force_tests.assignment": Assignment}
    monkeypatch.setattr(importer_module, "_model_for", test_models.__getitem__)
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(FragileObject)
        schema_editor.create_model(Assignment)
    yield
    with connection.schema_editor() as schema_editor:
        schema_editor.delete_model(Assignment)
        schema_editor.delete_model(FragileObject)


def archive(*records):
    return ParsedArchive(
        manifest={"source_instance": str(uuid.uuid4())},
        records=list(records),
        assets={},
        warnings=[],
    )


def fragile(pk, name):
    return {
        "id": f"force_tests.fragileobject:{pk}",
        "model": "force_tests.fragileobject",
        "source_pk": str(pk),
        "fields": {"name": name},
        "relations": {},
    }


def test_failing_record_aborts_import_without_force(force_tables):
    with pytest.raises(ArchiveValidationError, match="kaputt"):
        import_archive(archive(fragile(1, "ok"), fragile(2, "bad")), conflict_strategy="update", dry_run=False)
    assert FragileObject.objects.count() == 0


def test_force_mode_skips_only_the_failing_record(force_tables):
    report = import_archive(
        archive(fragile(1, "ok"), fragile(2, "bad")),
        conflict_strategy="update",
        dry_run=False,
        force=True,
    )

    assert list(FragileObject.objects.values_list("name", flat=True)) == ["ok"]
    assert report.created == 1
    assert report.skipped == 1
    assert any("force_tests.fragileobject:2" in warning and "kaputt" in warning for warning in report.warnings)


def test_dangling_generic_reference_skips_record_instead_of_writing_null(force_tables):
    record = {
        "id": "force_tests.assignment:1",
        "model": "force_tests.assignment",
        "source_pk": "1",
        "fields": {},
        "relations": {},
        "generic_relations": {"assigned_object": None},
    }

    report = import_archive(archive(record), conflict_strategy="update", dry_run=False)

    assert Assignment.objects.count() == 0
    assert report.skipped == 1
