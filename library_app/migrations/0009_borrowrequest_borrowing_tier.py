from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('library_app', '0008_borrowrequest_requested_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='borrowrequest',
            name='borrowing_tier',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='borrow_requests',
                to='library_app.borrowingtier',
            ),
        ),
        migrations.AlterField(
            model_name='borrowrequest',
            name='requested_days',
            field=models.PositiveSmallIntegerField(default=14, help_text='Borrowing duration selected by the librarian.'),
        ),
    ]
