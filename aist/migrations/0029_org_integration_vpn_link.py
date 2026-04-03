from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aist', '0028_add_vpn_tls_key_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='orgintegration',
            name='vpn_integration',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='dependent_integrations',
                limit_choices_to={'integration_type': 'VPN'},
                to='aist.orgintegration',
                help_text=(
                    'VPN integration to route this integration\'s requests through. '
                    'Must be a VPN-type integration in the same organization.'
                ),
            ),
        ),
    ]
