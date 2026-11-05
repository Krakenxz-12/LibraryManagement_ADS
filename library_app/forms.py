from django import forms

from .models import LibrarySettings


class LibrarySettingsForm(forms.ModelForm):
    """Validated by the model's min/max validators (no negative fees, no zero limits, etc.)."""

    class Meta:
        model = LibrarySettings
        fields = ['daily_fine_rate', 'weekly_borrow_limit', 'borrow_period_days']
        widgets = {
            'daily_fine_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'weekly_borrow_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'max': '50'}),
            'borrow_period_days': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'max': '365'}),
        }
        help_texts = {
            'daily_fine_rate': 'Charged for every day a book is returned late. 0 means no fines.',
            'weekly_borrow_limit': 'Counted when the librarian confirms the physical handover (Monday to Sunday).',
            'borrow_period_days': 'The due date is counted from the day the book is actually collected.',
        }
