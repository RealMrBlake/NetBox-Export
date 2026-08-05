import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class InstanceIdentity(models.Model):
    """Stable identity used to correlate repeat exports from this NetBox."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    singleton = models.BooleanField(default=True, unique=True, editable=False)

    @classmethod
    def local_id(cls):
        return cls.objects.get_or_create(singleton=True)[0].pk


class ImportedObjectMapping(models.Model):
    """Maps an object from another NetBox instance to its local counterpart."""

    source_instance = models.UUIDField()
    source_model = models.CharField(max_length=100)
    source_object_id = models.CharField(max_length=255)
    target_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_id = models.CharField(max_length=255)
    target = GenericForeignKey("target_type", "target_id")
    last_imported = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("source_instance", "source_model", "source_object_id"),
                name="netbox_export_unique_source_object",
            ),
        )
        indexes = (
            models.Index(fields=("target_type", "target_id"), name="netbox_exp_target__d4d805_idx"),
        )
