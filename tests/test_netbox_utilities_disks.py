import uuid
from typing import ClassVar

import pytest
from django.db import connection, models

from netbox_export.services import graph as graph_module
from netbox_export.services import importer as importer_module
from netbox_export.services.archive import ParsedArchive
from netbox_export.services.importer import _find_existing, import_archive
from netbox_export.services.references import ReferenceResolver

pytestmark = pytest.mark.django_db(transaction=True)


class DiskDeviceType(models.Model):
    class Meta:
        app_label = "disk_tests"


class DiskDevice(models.Model):
    device_type = models.ForeignKey(DiskDeviceType, on_delete=models.PROTECT)

    class Meta:
        app_label = "disk_tests"


class DiskBayTemplate(models.Model):
    device_type = models.OneToOneField(DiskDeviceType, on_delete=models.CASCADE)
    slot_count = models.PositiveSmallIntegerField()

    class Meta:
        app_label = "netbox_utilities"


class Disk(models.Model):
    device = models.ForeignKey(DiskDevice, on_delete=models.CASCADE)
    slot = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=20, default="active")
    serial = models.CharField(max_length=100, blank=True)

    class Meta:
        app_label = "netbox_utilities"
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=("device", "slot"),
                condition=~models.Q(status="replaced"),
                name="disk_tests_unique_occupied_slot",
            )
        ]


MODELS = (DiskDeviceType, DiskDevice, DiskBayTemplate, Disk)


@pytest.fixture
def disk_tables():
    with connection.schema_editor() as schema_editor:
        for model in MODELS:
            schema_editor.create_model(model)
    yield
    with connection.schema_editor() as schema_editor:
        for model in reversed(MODELS):
            schema_editor.delete_model(model)


def disk_record(pk, status, serial):
    return {
        "id": f"netbox_utilities.disk:{pk}",
        "model": "netbox_utilities.disk",
        "source_pk": str(pk),
        "fields": {"slot": 0, "status": status, "serial": serial},
        "relations": {"device": {"ref": "disk_tests.diskdevice:1"}},
    }


def test_replaced_disk_does_not_match_occupied_slot(disk_tables, monkeypatch):
    device = DiskDevice.objects.create(device_type=DiskDeviceType.objects.create())
    active = Disk.objects.create(device=device, slot=0, status="active", serial="NEW")
    monkeypatch.setattr(importer_module, "_mapped_object", lambda *args: None)
    resolver = ReferenceResolver({"disk_tests.diskdevice:1": device}, [], lambda label: None)

    assert _find_existing(uuid.uuid4(), Disk, disk_record(1, "replaced", "OLD"), resolver) is None
    assert _find_existing(uuid.uuid4(), Disk, disk_record(2, "active", "NEW"), resolver) == active


def test_disk_bay_template_is_exported_with_device_type(disk_tables, monkeypatch):
    device_type = DiskDeviceType.objects.create()
    template = DiskBayTemplate.objects.create(device_type=device_type, slot_count=12)
    device = DiskDevice.objects.create(device_type=device_type)
    disk = Disk.objects.create(device=device, slot=0)
    monkeypatch.setattr(graph_module, "exportable_models", lambda: MODELS)

    graph = graph_module.ObjectGraph(max_objects=100).collect([device])

    assert graph_module.object_key(disk) in graph.members
    assert graph_module.object_key(device_type) in graph.dependencies
    assert graph_module.object_key(template) in graph.dependencies


def test_netbox_utilities_records_are_skipped_without_plugin():
    parsed = ParsedArchive(
        manifest={"source_instance": str(uuid.uuid4())},
        records=[
            {
                "id": "netbox_utilities.raidgroup:1",
                "model": "netbox_utilities.raidgroup",
                "source_pk": "1",
                "fields": {"name": "R1"},
                "relations": {},
            }
        ],
        assets={},
        warnings=[],
    )

    report = import_archive(parsed, conflict_strategy="update", dry_run=True)

    assert report.skipped == 1
    assert any("netbox_utilities.raidgroup" in warning for warning in report.warnings)
