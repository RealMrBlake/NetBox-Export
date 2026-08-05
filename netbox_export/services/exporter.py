from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from django.conf import settings

from netbox_export.models import InstanceIdentity

from .archive import build_archive
from .codec import serialize_object
from .graph import ObjectGraph, seed_scope


def export_scope(scope_type: str, scope_id: int, *, max_objects: int, signing_key: str = ""):
    root, seeds = seed_scope(scope_type, scope_id)
    graph = ObjectGraph(max_objects=max_objects).collect(seeds)
    objects = graph.objects
    assets = {}
    records = [
        serialize_object(obj, set(objects), assets)
        for _, obj in sorted(objects.items())
    ]
    counts = Counter(record["model"] for record in records)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_instance": str(InstanceIdentity.local_id()),
        "source_netbox_version": getattr(getattr(settings, "RELEASE", None), "version", "4.6"),
        "plugin_version": "0.2.0",
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
