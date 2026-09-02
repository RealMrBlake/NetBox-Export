import datetime
import decimal
import uuid
from dataclasses import dataclass

import pytest

from netbox_export.services import codec
from netbox_export.services.codec import decode_scalar, encode_scalar


@dataclass
class FakeRange:
    lower: object = None
    upper: object = None
    bounds: str = "[)"
    empty: bool = False

    @property
    def lower_inc(self):
        return not self.empty and self.bounds[0] == "["

    @property
    def upper_inc(self):
        return not self.empty and self.bounds[1] == "]"

    @property
    def isempty(self):
        return self.empty


def test_scalar_round_trip():
    value = {
        "decimal": decimal.Decimal("12.340"),
        "uuid": uuid.UUID("00112233-4455-6677-8899-aabbccddeeff"),
        "date": datetime.date(2026, 8, 5),
        "datetime": datetime.datetime(2026, 8, 5, 10, 30, tzinfo=datetime.UTC),
        "duration": datetime.timedelta(seconds=42),
        "bytes": b"binary",
    }

    assert decode_scalar(encode_scalar(value)) == value


def test_postgresql_range_round_trip(monkeypatch):
    monkeypatch.setattr(
        codec,
        "_make_range",
        lambda lower, upper, bounds, empty: FakeRange(lower, upper, bounds, empty),
    )
    value = FakeRange(1, 56, "[)")

    encoded = encode_scalar(value)

    assert encoded == {
        "$type": "range",
        "lower": 1,
        "upper": 56,
        "bounds": "[)",
        "empty": False,
    }
    assert decode_scalar(encoded) == value


def test_real_psycopg_range_round_trip():
    pytest.importorskip("psycopg")
    from django.db.backends.postgresql.psycopg_any import NumericRange

    value = NumericRange(1, 56, "[)")

    assert decode_scalar(encode_scalar(value)) == value
