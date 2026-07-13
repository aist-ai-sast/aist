# Adds two small, single-purpose access-control models:
#
# - OrgMemberAccessScope: an explicit, persisted "restricted" flag per
#   (organization, user) — set ONLY by an explicit restricted-invite or
#   reset-to-full-access action, never inferred from Product_Member row
#   counts (a full member can legitimately have Product_Member rows too —
#   see ProjectAccessDenial below).
# - ProjectAccessDenial: an explicit "No access" override for ONE
#   (project, user) pair, independent of every other project — how a full
#   member's single-project denial is represented without narrowing their
#   access to every other project.
#
# Feature not yet shipped — no backfill needed: restricted-ness is no longer
# inferred from existing Product_Member rows under the new model, so there is
# nothing to migrate forward.
from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0037_auth_user_email_ci_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrgMemberAccessScope",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("restricted", models.BooleanField(default=False)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="member_access_scopes", to="aist.organization")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="org_access_scopes", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Org member access scope",
                "verbose_name_plural": "Org member access scopes",
                "unique_together": {("organization", "user")},
            },
        ),
        migrations.CreateModel(
            name="ProjectAccessDenial",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="access_denials", to="aist.aistproject")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_access_denials", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Project access denial",
                "verbose_name_plural": "Project access denials",
                "unique_together": {("project", "user")},
            },
        ),
    ]
