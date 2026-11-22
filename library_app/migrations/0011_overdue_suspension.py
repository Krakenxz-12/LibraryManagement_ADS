from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library_app', '0010_bookreview'),
    ]

    operations = [
        migrations.AddField(
            model_name='member',
            name='suspension_strikes',
            field=models.PositiveSmallIntegerField(default=0, help_text='Consecutive suspensions for unreturned books. Reset by returning a book on time.'),
        ),
        migrations.AddField(
            model_name='member',
            name='auto_suspended',
            field=models.BooleanField(default=False, help_text='True when the system (not a librarian) suspended this member.'),
        ),
        migrations.AddField(
            model_name='member',
            name='suspended_until',
            field=models.DateTimeField(blank=True, null=True, help_text='Earliest automatic reactivation (repeat offenders are locked for a week).'),
        ),
        migrations.AddField(
            model_name='borrowtransaction',
            name='suspension_counted',
            field=models.BooleanField(default=False),
        ),
    ]
