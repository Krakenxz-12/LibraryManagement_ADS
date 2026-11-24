from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library_app', '0012_book_copies'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='middle_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
