import django.db.models.deletion
from django.db import migrations, models


def create_aist_test_meta(apps, schema_editor):
    Test = apps.get_model("dojo", "Test")
    AISTTestMeta = apps.get_model("aist", "AISTTestMeta")
    TestDeduplicationProgress = apps.get_model("aist", "TestDeduplicationProgress")

    test_ids = list(Test.objects.values_list("id", flat=True))
    if not test_ids:
        return

    AISTTestMeta.objects.bulk_create(
        [AISTTestMeta(test_id=tid) for tid in test_ids],
        ignore_conflicts=True,
    )

    field_names = {field.name for field in Test._meta.get_fields()}
    if "deduplication_complete" in field_names:
        AISTTestMeta.objects.filter(test__deduplication_complete=True).update(
            deduplication_complete=True,
        )
        return

    done_ids = list(
        TestDeduplicationProgress.objects.filter(deduplication_complete=True)
        .values_list("test_id", flat=True),
    )
    if done_ids:
        AISTTestMeta.objects.filter(test_id__in=done_ids).update(
            deduplication_complete=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AISTTestMeta",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("deduplication_complete", models.BooleanField(default=False)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "test",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aist_meta",
                        to="dojo.test",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="aisttestmeta",
            index=models.Index(fields=["deduplication_complete"], name="aist_aistt_dedup_0a28a4_idx"),
        ),
        migrations.RunPython(create_aist_test_meta, reverse_code=migrations.RunPython.noop),
    ]
