import io

from django.core.files.storage import InMemoryStorage
from django.db import models
from PIL import Image

from netbox_export.services.importer import _cleanup_files, _set_files


class ImageAsset(models.Model):
    image = models.ImageField(
        upload_to="test-images",
        height_field="image_height",
        width_field="image_width",
    )
    image_height = models.PositiveSmallIntegerField()
    image_width = models.PositiveSmallIntegerField()

    class Meta:
        app_label = "file_import_tests"


def image_bytes(width=3, height=2):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(output, format="PNG")
    return output.getvalue()


def image_record():
    return {
        "files": {
            "image": {
                "path": "assets/extras.imageattachment_17/image/server-room.png",
                "name": "image-attachments/location_2_Serverraum.jpg",
            }
        }
    }


def test_dry_run_derives_required_image_dimensions_without_storing_file():
    obj = ImageAsset()
    assets = {"assets/extras.imageattachment_17/image/server-room.png": image_bytes()}
    saved_files = []

    _set_files(obj, image_record(), assets, saved_files, dry_run=True)

    assert (obj.image_width, obj.image_height) == (3, 2)
    assert not obj.image
    assert saved_files == []


def test_file_import_keeps_derived_dimensions_after_storage_save(monkeypatch):
    field = ImageAsset._meta.get_field("image")
    storage = InMemoryStorage()
    monkeypatch.setattr(field, "storage", storage)
    obj = ImageAsset()
    assets = {"assets/extras.imageattachment_17/image/server-room.png": image_bytes(5, 4)}
    saved_files = []

    _set_files(obj, image_record(), assets, saved_files, dry_run=False)

    assert (obj.image_width, obj.image_height) == (5, 4)
    assert storage.exists(obj.image.name)
    assert saved_files == [(storage, obj.image.name)]

    _cleanup_files(saved_files)
    assert not storage.exists(obj.image.name)
