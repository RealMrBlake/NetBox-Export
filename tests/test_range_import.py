from dataclasses import dataclass

from netbox_export.services.importer import _coerce_model_field_value


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


class IntegerField:
    @staticmethod
    def to_python(value):
        return int(value)


class IntegerRangeField:
    base_field = IntegerField()
    range_type = FakeRange


class ArrayField:
    base_field = IntegerRangeField()


def test_legacy_string_ranges_are_restored_for_range_array_fields():
    value = ["[1, 56)", "(100, 200]", "empty"]

    converted = _coerce_model_field_value(ArrayField(), value)

    assert converted == [
        FakeRange(1, 56, "[)"),
        FakeRange(100, 200, "(]"),
        FakeRange(empty=True),
    ]


def test_range_like_strings_remain_unchanged_for_normal_fields():
    field = object()

    assert _coerce_model_field_value(field, "[1, 56)") == "[1, 56)"
