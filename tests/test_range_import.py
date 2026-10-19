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


class BaseIPField:
    @staticmethod
    def to_python(value):
        return ("parsed", value)


class IPAddressField(BaseIPField):
    pass


class CharField:
    @staticmethod
    def to_python(value):
        raise AssertionError("plain fields must not be converted")


def test_netbox_ip_field_values_are_converted_for_model_save():
    assert _coerce_model_field_value(IPAddressField(), "192.0.2.1/24") == ("parsed", "192.0.2.1/24")


def test_other_string_values_are_left_unchanged():
    assert _coerce_model_field_value(CharField(), "192.0.2.1/24") == "192.0.2.1/24"
