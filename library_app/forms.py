from django import forms

from .models import BookReview, LibrarySettings, Member
from .profanity import contains_profanity, find_contact_or_link


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


class MemberSelfProfileForm(forms.ModelForm):
    """Fields a signed-in member is allowed to maintain on their own profile."""

    class Meta:
        model = Member
        fields = ['first_name', 'last_name', 'phone', 'address', 'date_of_birth']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'given-name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'family-name'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'tel', 'placeholder': 'e.g. 09XXXXXXXXX'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'autocomplete': 'street-address'}),
            'date_of_birth': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }
        labels = {
            'first_name': 'First name',
            'last_name': 'Last name',
            'phone': 'Phone number',
            'address': 'Home address',
            'date_of_birth': 'Date of birth',
        }


class BookReviewForm(forms.ModelForm):
    """Rating (1-5) plus an optional comment. The comment is screened before it is saved."""
    rating = forms.TypedChoiceField(
        choices=[(i, str(i)) for i in range(5, 0, -1)], coerce=int, empty_value=None,
        widget=forms.RadioSelect, error_messages={'required': 'Please choose a star rating from 1 to 5.'},
    )

    class Meta:
        model = BookReview
        fields = ['rating', 'comment']
        widgets = {
            'comment': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3, 'maxlength': 1000,
                'placeholder': 'Share what you thought about this book (optional)',
            }),
        }

    def clean_comment(self):
        comment = ' '.join((self.cleaned_data.get('comment') or '').split())  # trim + collapse whitespace
        if contains_profanity(comment):
            raise forms.ValidationError(
                'Your comment contains language that is not allowed. Please reword it and try again.',
                code='profanity',
            )
        if find_contact_or_link(comment):
            raise forms.ValidationError(
                'Please remove links, e-mail addresses and phone numbers from your comment.',
                code='contact_or_link',
            )
        return comment
