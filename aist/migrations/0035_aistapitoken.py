import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("aist", "0034_gitea_integration"),
    ]

    operations = [
        migrations.CreateModel(
            name="AISTApiToken",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                (
                    "scope",
                    models.CharField(
                        choices=[("read_only", "Read only"), ("read_write", "Read and write")],
                        default="read_only",
                        max_length=32,
                    ),
                ),
                ("public_id", models.CharField(max_length=32, unique=True)),
                ("secret_hash", models.CharField(max_length=128)),
                ("last4", models.CharField(max_length=4)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aist_api_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "AIST API token",
            },
        ),
        migrations.AddIndex(
            model_name="aistapitoken",
            index=models.Index(fields=["user"], name="aist_api_token_user_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="aistapitoken",
            unique_together={("user", "name")},
        ),
    ]
