import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="InstanceIdentity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name="ImportedObjectMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_instance", models.UUIDField()),
                ("source_model", models.CharField(max_length=100)),
                ("source_object_id", models.CharField(max_length=255)),
                ("target_id", models.CharField(max_length=255)),
                ("last_imported", models.DateTimeField(auto_now=True)),
                (
                    "target_type",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype"),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["target_type", "target_id"], name="netbox_exp_target__d4d805_idx")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("source_instance", "source_model", "source_object_id"),
                        name="netbox_export_unique_source_object",
                    )
                ],
            },
        ),
    ]

