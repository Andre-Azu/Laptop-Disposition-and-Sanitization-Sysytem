from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("laptops", "0025_alter_itassessment_q10_1_alter_itassessment_q10_2_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="is_listed",
            field=models.BooleanField(default=False),
        ),
    ]
