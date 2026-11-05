from django import forms

from .models import LibrarySettings


class LibrarySettingsForm(forms.ModelForm):
    """Validated by the model's min/max validators (no negative fees, no zero limits, etc.)."""

    class Meta:
        model = LibrarySettings
        fields = ['daily_fine_rate', 'weekly_borrow_limit', 'max_renewals']
        widgets = {
            'daily_fine_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'weekly_borrow_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'max': '50'}),
            'max_renewals': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'max': '5'}),
        }
        help_texts = {
            'daily_fine_rate': 'Charged for every day a book is returned late. 0 means no fines.',
            'weekly_borrow_limit': 'Counted when the librarian confirms the physical handover (Monday to Sunday).',
            'max_renewals': 'Each renewal adds the loan\'s own length again. Only for books that are not overdue or reserved. 0 turns renewals off.',
        }
