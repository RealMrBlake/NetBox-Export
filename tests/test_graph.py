import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from netbox_export.services import graph as graph_module

pytestmark = pytest.mark.django_db(transaction=True)


class GraphParent(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "graph_tests"


class GraphReference(models.Model):
    code = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "graph_tests"


class GraphChild(models.Model):
    parent = models.ForeignKey(GraphParent, on_delete=models.CASCADE)
    reference = models.ForeignKey(GraphReference, on_delete=models.PROTECT)

    class Meta:
        app_label = "graph_tests"


class GraphDetail(models.Model):
    child = models.ForeignKey(GraphChild, on_delete=models.CASCADE)

    class Meta:
        app_label = "graph_tests"


class PrivateGraphModel(models.Model):
    _netbox_private = True

    class Meta:
        app_label = "graph_tests"


class PortMapping(models.Model):
    _netbox_private = True

    class Meta:
        app_label = "dcim"


class CablePath(models.Model):
    _netbox_private = True

    class Meta:
        app_label = "dcim"


def test_private_plugin_models_are_not_exportable():
    assert graph_module.is_exportable_model(PrivateGraphModel) is False


def test_private_port_mappings_are_exported_but_cable_paths_are_not():
    assert graph_module.is_exportable_model(PortMapping) is True
    assert graph_module.is_exportable_model(CablePath) is False


def test_batched_graph_collects_members_and_dependencies(monkeypatch):
    graph_models = (GraphParent, GraphReference, GraphChild, GraphDetail)
    with connection.schema_editor() as schema_editor:
        for model in graph_models:
            schema_editor.create_model(model)
    try:
        reference = GraphReference.objects.create(code="shared")
        parent = GraphParent.objects.create(name="scope")
        children = [GraphChild(parent=parent, reference=reference) for _ in range(20)]
        GraphChild.objects.bulk_create(children)
        details = [GraphDetail(child=child) for child in children]
        GraphDetail.objects.bulk_create(details)
        monkeypatch.setattr(graph_module, "exportable_models", lambda: graph_models)

        with CaptureQueriesContext(connection) as queries:
            graph = graph_module.ObjectGraph(max_objects=100, query_batch_size=100).collect([parent])
            loaded_keys = {graph_module.object_key(obj) for obj in graph.iter_loaded_objects()}

        assert len(graph.members) == 41
        assert graph_module.object_key(parent) in graph.members
        assert all(graph_module.object_key(child) in graph.members for child in children)
        assert all(graph_module.object_key(detail) in graph.members for detail in details)
        assert set(graph.dependencies) == {graph_module.object_key(reference)}
        assert loaded_keys == set(graph.objects)
        assert len(queries) < 30
    finally:
        with connection.schema_editor() as schema_editor:
            for model in reversed(graph_models):
                schema_editor.delete_model(model)
