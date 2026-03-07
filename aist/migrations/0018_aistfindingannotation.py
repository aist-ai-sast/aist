from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0017_findingevent_pgh_obj_id_pgh_id_index"),
        ("dojo", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AISTFindingAnnotation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "finding",
                    models.OneToOneField(
                        db_index=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aist_annotation",
                        to="dojo.finding",
                    ),
                ),
                ("is_regression", models.BooleanField(
                    default=False,
                    help_text="True when the finding re-appeared after being previously mitigated.",
                )),
                ("regression_detected_at", models.DateTimeField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Finding annotation"},
        ),
        migrations.AddIndex(
            model_name="aistfindingannotation",
            index=models.Index(fields=["is_regression"], name="aist_annotation_regression_idx"),
        ),
    ]
