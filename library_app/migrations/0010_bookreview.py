from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('library_app', '0009_borrowrequest_borrowing_tier'),
    ]

    operations = [
        migrations.CreateModel(
            name='BookReview',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rating', models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])),
                ('comment', models.TextField(blank=True, max_length=1000)),
                ('is_hidden', models.BooleanField(default=False, help_text='Hidden by staff; not shown to members.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('book', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviews', to='library_app.book')),
                ('member', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviews', to='library_app.member')),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='bookreview',
            constraint=models.UniqueConstraint(fields=('book', 'member'), name='one_review_per_member_per_book'),
        ),
        migrations.AddConstraint(
            model_name='bookreview',
            constraint=models.CheckConstraint(condition=models.Q(('rating__gte', 1), ('rating__lte', 5)), name='review_rating_1_to_5'),
        ),
    ]
