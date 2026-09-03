import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0049_dast_terminal_quality"),
    ]

    operations = [
        migrations.AddField(
            model_name="aistpipeline",
            name="dast_binding",
            field=models.ForeignKey(
                blank=True,
                help_text="DAST target binding selected for this pipeline.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pipelines",
                to="aist.dastprojectbinding",
            ),
        ),
    ]
