from types import SimpleNamespace
from unittest.mock import MagicMock

from netbox_export.services import importer
from netbox_export.services.importer import (
    _rebuild_imported_cable_paths,
    _release_port_mapping_conflicts,
)


class FakeQuerySet(list):
    def exclude(self, **kwargs):
        return FakeQuerySet(item for item in self if item.pk != kwargs["pk"])

    def order_by(self, *args):
        return self


class FakeManager:
    def __init__(self, objects):
        self.objects = FakeQuerySet(objects)
        self.filters = []

    def select_for_update(self):
        return self

    def filter(self, *args, **kwargs):
        self.filters.append((args, kwargs))
        return FakeQuerySet(self.objects)


def test_port_mapping_conflicts_are_replaced_before_save():
    deleted = []
    conflict = SimpleNamespace(pk=12, delete=lambda: deleted.append(12))

    class PortMapping:
        _meta = SimpleNamespace(label_lower="dcim.portmapping")
        _default_manager = FakeManager([conflict])

    mapping = PortMapping()
    mapping.pk = 11
    mapping.front_port_id = 21
    mapping.front_port_position = 1
    mapping.rear_port_id = 31
    mapping.rear_port_position = 1
    resolver = SimpleNamespace(warn=MagicMock())

    _release_port_mapping_conflicts(mapping, resolver, "dcim.portmapping:99")

    assert deleted == [12]
    resolver.warn.assert_called_once()
    assert "front/rear port cabling" in resolver.warn.call_args.args[1]


def test_imported_cable_terminations_trigger_netbox_path_rebuild(monkeypatch):
    cables = [SimpleNamespace(pk=7), SimpleNamespace(pk=9)]

    class Cable:
        _default_manager = FakeManager(cables)

    trace_paths = SimpleNamespace(send=MagicMock())
    monkeypatch.setattr(importer, "_model_for", lambda label: Cable)
    monkeypatch.setattr(importer, "_trace_paths_signal", lambda: trace_paths)
    records = {
        "dcim.cabletermination:1": {"model": "dcim.cabletermination"},
        "dcim.cabletermination:2": {"model": "dcim.cabletermination"},
        "dcim.device:3": {"model": "dcim.device"},
    }
    resolved = {
        "dcim.cabletermination:1": SimpleNamespace(cable_id=7),
        "dcim.cabletermination:2": SimpleNamespace(cable_id=9),
        "dcim.device:3": object(),
    }

    _rebuild_imported_cable_paths(records, resolved)

    assert all(cable._terminations_modified is True for cable in cables)
    assert trace_paths.send.call_count == 2
    trace_paths.send.assert_any_call(Cable, instance=cables[0], created=False)
    trace_paths.send.assert_any_call(Cable, instance=cables[1], created=False)
