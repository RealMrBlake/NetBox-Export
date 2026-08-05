from __future__ import annotations

from .codec import decode_scalar
from .exceptions import ArchiveValidationError

MISSING_REFERENCE = object()


class ReferenceResolver:
    def __init__(self, resolved, warnings, model_loader):
        self.resolved = resolved
        self.warnings = warnings
        self.model_loader = model_loader
        self.skipped = set()
        self._external_cache = {}
        self._warning_keys = set()

    def warn(self, key, message):
        if key not in self._warning_keys:
            self._warning_keys.add(key)
            self.warnings.append(message)

    def skip(self, record_id, model_label, field_names):
        self.skipped.add(record_id)
        fields = ", ".join(field_names)
        self.warn(
            ("skipped", record_id),
            f"{record_id} ({model_label}) wurde wegen fehlender Pflichtreferenz übersprungen: {fields}.",
        )

    def _external_object(self, spec):
        cache_key = repr(spec)
        if cache_key in self._external_cache:
            return self._external_cache[cache_key]
        model = self.model_loader(spec["model"])
        lookup = {key: decode_scalar(value) for key, value in spec.get("lookup", {}).items()}
        reason = None
        try:
            value = model._default_manager.get(**lookup)
        except model.DoesNotExist:
            value = MISSING_REFERENCE
            reason = "nicht gefunden"
        except model.MultipleObjectsReturned:
            value = MISSING_REFERENCE
            reason = "nicht eindeutig"
        if value is MISSING_REFERENCE:
            self.warn(
                ("external", cache_key),
                f"Externe Referenz ausgelassen: {spec['model']} mit {lookup} ({reason}).",
            )
        self._external_cache[cache_key] = value
        return value

    def resolve(self, spec):
        if spec is None:
            return None, True
        if "ref" in spec:
            record_id = spec["ref"]
            if record_id in self.skipped:
                self.warn(
                    ("internal", record_id),
                    f"Referenzen auf das übersprungene Objekt {record_id} wurden ausgelassen.",
                )
                return MISSING_REFERENCE, True
            return self.resolved.get(record_id), record_id in self.resolved
        if "external" in spec:
            return self._external_object(spec["external"]), True
        raise ArchiveValidationError("Eine Objektreferenz im Archiv ist ungültig.")
