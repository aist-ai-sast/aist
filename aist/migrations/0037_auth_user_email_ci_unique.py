# Adds a real DB-level constraint backing the app-level email-uniqueness checks
# already added in AISTMeSerializer/OrganizationMembershipService. auth_user is
# Django's built-in model (no custom AUTH_USER_MODEL is configured in this
# project), so it can't be altered via a normal per-app AddConstraint operation
# from here — migrations.RunSQL is the standard, framework-supported way to
# constrain a model owned by another app. Partial index: blank emails are
# legitimate and must not collide with each other.
from django.db import migrations

CREATE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS aist_auth_user_email_ci_unique "
    "ON auth_user (LOWER(email)) WHERE email <> '';"
)
DROP_SQL = "DROP INDEX IF EXISTS aist_auth_user_email_ci_unique;"


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0036_org_membership_history"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
