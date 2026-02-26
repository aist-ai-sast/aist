from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0015_organization_product_type_metadata"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aistproject",
            name="product",
            field=models.OneToOneField(on_delete=models.deletion.CASCADE, to="dojo.product"),
        ),
    ]
