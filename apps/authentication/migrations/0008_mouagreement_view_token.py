import uuid
from django.db import migrations, models


def populate_view_tokens(apps, schema_editor):
    MOUAgreement = apps.get_model('authentication', 'MOUAgreement')
    for mou in MOUAgreement.objects.filter(view_token=None):
        mou.view_token = uuid.uuid4()
        mou.save(update_fields=['view_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0007_mouagreement_mou_pdf_s3_key'),
    ]

    operations = [
        # Step 1: add the column as nullable so existing rows don't need a value yet
        migrations.AddField(
            model_name='mouagreement',
            name='view_token',
            field=models.UUIDField(null=True, blank=True),
        ),
        # Step 2: give every existing row its own unique UUID
        migrations.RunPython(populate_view_tokens, migrations.RunPython.noop),
        # Step 3: make it non-nullable, unique, with a default for future rows
        migrations.AlterField(
            model_name='mouagreement',
            name='view_token',
            field=models.UUIDField(default=uuid.uuid4, unique=True, db_index=True),
        ),
    ]
