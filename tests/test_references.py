from netbox_export.services.references import MISSING_REFERENCE, ReferenceResolver


class DoesNotExist(Exception):
    pass


class MultipleObjectsReturned(Exception):
    pass


class FakeManager:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def get(self, **lookup):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_model(manager):
    return type(
        "FakeModel",
        (),
        {
            "DoesNotExist": DoesNotExist,
            "MultipleObjectsReturned": MultipleObjectsReturned,
            "_default_manager": manager,
        },
    )


def test_missing_external_reference_is_omitted_and_warned_once():
    manager = FakeManager(DoesNotExist())
    model = fake_model(manager)
    warnings = []
    resolver = ReferenceResolver({}, warnings, lambda label: model)
    spec = {"external": {"model": "users.user", "lookup": {"username": "lke"}}}

    first, first_available = resolver.resolve(spec)
    second, second_available = resolver.resolve(spec)

    assert first is MISSING_REFERENCE
    assert second is MISSING_REFERENCE
    assert first_available is True
    assert second_available is True
    assert manager.calls == 1
    assert warnings == [
        "Externe Referenz ausgelassen: users.user mit {'username': 'lke'} (nicht gefunden)."
    ]


def test_ambiguous_external_reference_is_omitted():
    manager = FakeManager(MultipleObjectsReturned())
    model = fake_model(manager)
    warnings = []
    resolver = ReferenceResolver({}, warnings, lambda label: model)

    value, available = resolver.resolve(
        {"external": {"model": "users.group", "lookup": {"name": "NetBox"}}}
    )

    assert value is MISSING_REFERENCE
    assert available is True
    assert warnings == [
        "Externe Referenz ausgelassen: users.group mit {'name': 'NetBox'} (nicht eindeutig)."
    ]


def test_reference_to_skipped_record_is_omitted_and_warned_once():
    warnings = []
    resolver = ReferenceResolver({}, warnings, lambda label: None)
    resolver.skip("dcim.rackreservation:17", "dcim.rackreservation", ["user"])

    first, first_available = resolver.resolve({"ref": "dcim.rackreservation:17"})
    second, second_available = resolver.resolve({"ref": "dcim.rackreservation:17"})

    assert first is MISSING_REFERENCE
    assert second is MISSING_REFERENCE
    assert first_available is True
    assert second_available is True
    assert warnings == [
        (
            "dcim.rackreservation:17 (dcim.rackreservation) wurde wegen fehlender "
            "Pflichtreferenz übersprungen: user."
        ),
        "Referenzen auf das übersprungene Objekt dcim.rackreservation:17 wurden ausgelassen.",
    ]
