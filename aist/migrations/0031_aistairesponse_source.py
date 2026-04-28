from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("aist", "0030_project_integration_override_is_disabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="aistairesponse",
            name="source",
            field=models.CharField(
                choices=[
                    ("ai_triage", "AI Triage"),
                    ("agent_analyzer", "Agent Analyzer"),
                ],
                db_index=True,
                default="ai_triage",
                max_length=32,
            ),
        ),
    ]
