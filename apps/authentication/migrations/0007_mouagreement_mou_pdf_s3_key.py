from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0006_user_mou_signed_mouagreement'),
    ]

    operations = [
        migrations.AddField(
            model_name='mouagreement',
            name='mou_pdf_s3_key',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
