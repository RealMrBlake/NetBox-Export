import datetime
import decimal
import uuid

from netbox_export.services.codec import decode_scalar, encode_scalar


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

