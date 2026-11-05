from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library_app', '0007_borrowing_tiers'),
    ]

    operations = [
        migrations.AddField(
            model_name='borrowrequest',
            name='requested_days',
            field=models.PositiveSmallIntegerField(default=14, help_text='Borrowing duration requested by the member.'),
        ),
    ]
