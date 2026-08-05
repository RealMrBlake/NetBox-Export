from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest
from django.db import connection, models

from netbox_export.services.exceptions import ImportConflictError
from netbox_export.services.importer import (
    DeferredDevicePlacement,
    _apply_device_placements,
    _defer_device_placement,
    _save_device_placement,
    _stage_device_placement,
)
from netbox_export.services.plugin_compat import PluginCompatibility
from netbox_export.services.references import ReferenceResolver

pytestmark = pytest.mark.django_db(transaction=True)


class PlacementRack(models.Model):
    name = models.CharField(max_length=64)

    class Meta:
        app_label = "placement_tests"


class PlacementDeviceType(models.Model):
    u_height = models.DecimalField(max_digits=4, decimal_places=1, default=1)
    is_full_depth = models.BooleanField(default=False)

    class Meta:
        app_label = "placement_tests"


class PlacementDevice(models.Model):
    name = models.CharField(max_length=64)
    device_type = models.ForeignKey(PlacementDeviceType, on_delete=models.PROTECT)
    rack = models.ForeignKey(PlacementRack, on_delete=models.PROTECT, null=True)
    position = models.DecimalField(max_digits=4, decimal_places=1, null=True)
    face = models.CharField(max_length=16, null=True)

    class Meta:
        app_label = "placement_tests"
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=("rack", "position", "face"),
                name="placement_tests_unique_rack_position_face",
            )
        ]


@pytest.fixture
def placement_schema():
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(PlacementRack)
        schema_editor.create_model(PlacementDeviceType)
        schema_editor.create_model(PlacementDevice)
    try:
        yield
    finally:
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(PlacementDevice)
            schema_editor.delete_model(PlacementDeviceType)
            schema_editor.delete_model(PlacementRack)


def placement_context(device, rack, *, position="31.0", face="front"):
    record_id = "dcim.device:77"
    resolved = {record_id: device, "dcim.rack:2": rack}
    warnings = []
    resolver = ReferenceResolver(resolved, warnings, lambda label: None)
    compatibility = PluginCompatibility(warnings, dry_run=False, tenant_required=False)
    placement = DeferredDevicePlacement(
        rack_spec={"ref": "dcim.rack:2"},
        position=Decimal(position) if position is not None else None,
        face=face,
    )
    return record_id, resolved, warnings, resolver, compatibility, placement


def test_device_placement_is_removed_from_initial_record():
    model = SimpleNamespace(_meta=SimpleNamespace(label_lower="dcim.device"))
    record = {
        "fields": {"name": "Router 1", "position": {"$type": "decimal", "value": "31.0"}, "face": "front"},
        "relations": {"site": {"ref": "dcim.site:1"}, "rack": {"ref": "dcim.rack:2"}},
    }

    prepared, placement = _defer_device_placement(model, record)

    assert prepared["fields"] == {"name": "Router 1"}
    assert prepared["relations"] == {"site": {"ref": "dcim.site:1"}}
    assert placement == DeferredDevicePlacement(
        rack_spec={"ref": "dcim.rack:2"},
        position=Decimal("31.0"),
        face="front",
    )
    assert "position" in record["fields"]
    assert "rack" in record["relations"]


def test_saving_placement_persists_location_inherited_from_rack():
    location = object()
    rack = SimpleNamespace(location=location)
    device = SimpleNamespace(rack=None, position=None, face=None, location=None)
    saved = []
    compatibility = SimpleNamespace(save=lambda obj, **kwargs: saved.append((obj, kwargs)))

    _save_device_placement(device, rack, Decimal("31.0"), "front", compatibility)

    assert device.location is location
    assert saved == [
        (
            device,
            {"update_fields": ["rack", "position", "face", "location"]},
        )
    ]


def test_update_releases_exact_rack_occupant_and_places_imported_device(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    device_type = PlacementDeviceType.objects.create()
    occupant = PlacementDevice.objects.create(
        name="Existing", device_type=device_type, rack=rack, position=Decimal("31.0"), face="front"
    )
    imported = PlacementDevice.objects.create(name="Imported", device_type=device_type)
    record_id, resolved, warnings, resolver, compatibility, placement = placement_context(imported, rack)

    _apply_device_placements(
        [(record_id, placement)],
        resolved,
        resolver,
        compatibility,
        conflict_strategy="update",
    )

    occupant.refresh_from_db()
    imported.refresh_from_db()
    assert (occupant.rack, occupant.position, occupant.face) == (rack, None, None)
    assert (imported.rack, imported.position, imported.face) == (rack, Decimal("31.0"), "front")
    assert len(warnings) == 1
    assert f"Zielgerät(e) {occupant.pk} (Existing)" in warnings[0]


def test_skip_keeps_occupant_and_leaves_imported_device_unpositioned(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    device_type = PlacementDeviceType.objects.create()
    occupant = PlacementDevice.objects.create(
        name="Existing", device_type=device_type, rack=rack, position=Decimal("31.0"), face="front"
    )
    imported = PlacementDevice.objects.create(name="Imported", device_type=device_type)
    record_id, resolved, warnings, resolver, compatibility, placement = placement_context(imported, rack)

    _apply_device_placements(
        [(record_id, placement)],
        resolved,
        resolver,
        compatibility,
        conflict_strategy="skip",
    )

    occupant.refresh_from_db()
    imported.refresh_from_db()
    assert (occupant.rack, occupant.position, occupant.face) == (rack, Decimal("31.0"), "front")
    assert (imported.rack, imported.position, imported.face) == (rack, None, None)
    assert len(warnings) == 1
    assert "wurde ohne Position im Rack gespeichert" in warnings[0]


def test_fail_reports_rack_conflict_before_database_constraint(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    device_type = PlacementDeviceType.objects.create()
    PlacementDevice.objects.create(
        name="Existing", device_type=device_type, rack=rack, position=Decimal("31.0"), face="front"
    )
    imported = PlacementDevice.objects.create(name="Imported", device_type=device_type)
    record_id, resolved, _, resolver, compatibility, placement = placement_context(imported, rack)

    with pytest.raises(ImportConflictError, match="Rackplatzkonflikt.*Rack R01"):
        _apply_device_placements(
            [(record_id, placement)],
            resolved,
            resolver,
            compatibility,
            conflict_strategy="fail",
        )


def test_half_depth_devices_can_share_position_on_opposite_faces(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    device_type = PlacementDeviceType.objects.create(is_full_depth=False)
    occupant = PlacementDevice.objects.create(
        name="Existing", device_type=device_type, rack=rack, position=Decimal("31.0"), face="front"
    )
    imported = PlacementDevice.objects.create(name="Imported", device_type=device_type)
    record_id, resolved, warnings, resolver, compatibility, placement = placement_context(
        imported, rack, face="rear"
    )

    _apply_device_placements(
        [(record_id, placement)],
        resolved,
        resolver,
        compatibility,
        conflict_strategy="update",
    )

    occupant.refresh_from_db()
    imported.refresh_from_db()
    assert occupant.position == Decimal("31.0")
    assert (imported.position, imported.face) == (Decimal("31.0"), "rear")
    assert warnings == []


def test_full_depth_multi_u_overlap_is_released(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    full_depth = PlacementDeviceType.objects.create(u_height=Decimal("2.0"), is_full_depth=True)
    half_depth = PlacementDeviceType.objects.create(u_height=Decimal("1.0"), is_full_depth=False)
    occupant = PlacementDevice.objects.create(
        name="Existing",
        device_type=full_depth,
        rack=rack,
        position=Decimal("30.5"),
        face="front",
    )
    imported = PlacementDevice.objects.create(name="Imported", device_type=half_depth)
    record_id, resolved, warnings, resolver, compatibility, placement = placement_context(
        imported, rack, position="31.0", face="rear"
    )

    _apply_device_placements(
        [(record_id, placement)],
        resolved,
        resolver,
        compatibility,
        conflict_strategy="update",
    )

    occupant.refresh_from_db()
    imported.refresh_from_db()
    assert occupant.position is None
    assert (imported.position, imported.face) == (Decimal("31.0"), "rear")
    assert len(warnings) == 1


def test_imported_devices_can_swap_rack_positions(placement_schema):
    rack = PlacementRack.objects.create(name="R01")
    device_type = PlacementDeviceType.objects.create()
    first = PlacementDevice.objects.create(
        name="First", device_type=device_type, rack=rack, position=Decimal("10.0"), face="front"
    )
    second = PlacementDevice.objects.create(
        name="Second", device_type=device_type, rack=rack, position=Decimal("20.0"), face="front"
    )
    for device in (first, second):
        _stage_device_placement(device)
        device.save(update_fields=["position", "face"])

    resolved = {
        "dcim.device:1": first,
        "dcim.device:2": second,
        "dcim.rack:2": rack,
    }
    warnings = []
    resolver = ReferenceResolver(resolved, warnings, lambda label: None)
    compatibility = PluginCompatibility(warnings, dry_run=False, tenant_required=False)
    placements = [
        (
            "dcim.device:1",
            DeferredDevicePlacement({"ref": "dcim.rack:2"}, Decimal("20.0"), "front"),
        ),
        (
            "dcim.device:2",
            DeferredDevicePlacement({"ref": "dcim.rack:2"}, Decimal("10.0"), "front"),
        ),
    ]

    _apply_device_placements(
        placements,
        resolved,
        resolver,
        compatibility,
        conflict_strategy="update",
    )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.position == Decimal("20.0")
    assert second.position == Decimal("10.0")
    assert warnings == []
