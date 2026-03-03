import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0007_mouagreement_mou_pdf_s3_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='mouagreement',
            name='view_token',
            field=models.UUIDField(default=uuid.uuid4, unique=True, db_index=True),
        ),
    ]
