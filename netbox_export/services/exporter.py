from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from django.conf import settings

from netbox_export.models import InstanceIdentity

from .archive import build_archive
from .codec import serialize_object
from .graph import ObjectGraph, seed_scope


def export_scope(
    scope_type: str,
    scope_id: int,
    *,
    max_objects: int,
    query_batch_size: int = 500,
    signing_key: str = "",
):
    root, seeds = seed_scope(scope_type, scope_id)
    graph = ObjectGraph(max_objects=max_objects, query_batch_size=query_batch_size).collect(seeds)
    objects = graph.objects
    exported_keys = set(objects)
    assets = {}
    records = [
        serialize_object(
            obj,
            exported_keys,
            assets,
            custom_fields=graph.custom_fields_for_model(type(obj)),
        )
        for obj in graph.iter_loaded_objects()
    ]
    counts = Counter(record["model"] for record in records)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_instance": str(InstanceIdentity.local_id()),
        "source_netbox_version": getattr(getattr(settings, "RELEASE", None), "version", "4.6"),
        "plugin_version": "0.3.14",
        "scope": {
            "type": scope_type,
            "source_pk": str(scope_id),
            "label": str(root),
        },
        "object_count": len(records),
        "member_count": len(graph.members),
        "dependency_count": len(graph.dependencies),
        "models": dict(sorted(counts.items())),
    }
    return build_archive(manifest, records, assets, signing_key=signing_key), manifest
