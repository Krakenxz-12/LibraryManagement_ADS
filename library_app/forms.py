import re

from django import forms
from django.contrib.auth.models import User

from .models import BookReview, LibrarySettings, Member
from .profanity import contains_profanity, find_contact_or_link


RESERVED_USERNAMES = {
    'admin', 'administrator', 'root', 'superuser', 'librarian', 'library', 'staff', 'support',
    'system', 'moderator', 'owner', 'help', 'null', 'none',
}
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,30}$')


def clean_new_username(raw, user=None):
    """Validate a username a member wants to switch to. Returns the cleaned value or raises ValidationError.
    `user` is the account being renamed (so keeping the current name is always allowed)."""
    username = (raw or '').strip()
    if user is not None and username == user.username:
        return username
    if not USERNAME_RE.match(username):
        raise forms.ValidationError(
            'Use 3 to 30 characters: letters, numbers, dots, dashes or underscores (no spaces).', code='invalid')
    if username.lower() in RESERVED_USERNAMES:
        raise forms.ValidationError('That username is reserved. Please choose another one.', code='reserved')
    if contains_profanity(username):
        raise forms.ValidationError('That username is not allowed. Please choose another one.', code='profanity')
    taken = User.objects.filter(username__iexact=username)
    if user is not None:
        taken = taken.exclude(pk=user.pk)
    if taken.exists():
        raise forms.ValidationError('That username is already taken.', code='taken')
    return username


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
    username = forms.CharField(
        label='Username', max_length=30,
        help_text='3 to 30 characters: letters, numbers, dots, dashes or underscores.',
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'username', 'maxlength': 30}),
    )
    field_order = ['username', 'first_name', 'last_name', 'phone', 'address', 'date_of_birth']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = getattr(self.instance, 'user', None)
        if self.account is not None:
            self.fields['username'].initial = self.account.username
        else:  # no login account linked: nothing to rename
            del self.fields['username']

    def clean_username(self):
        return clean_new_username(self.cleaned_data.get('username'), self.account)

    class Meta:
        model = Member
        fields = ['first_name', 'last_name', 'phone', 'address', 'date_of_birth']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'given-name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'family-name'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'tel', 'inputmode': 'numeric', 'placeholder': '09XXXXXXXXX', 'maxlength': '11', 'minlength': '11', 'pattern': '09[0-9]{9}', 'title': 'Enter an 11-digit Philippine mobile number starting with 09.'}),
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

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()

        if not phone.isascii() or not phone.isdigit():
            raise forms.ValidationError(
                "Phone number must contain digits only."
            )

        if len(phone) != 11 or not phone.startswith('09'):
            raise forms.ValidationError(
                "Enter a valid Philippine mobile number (09XXXXXXXXX)."
            )

        return phone



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


class AdminAccountForm(forms.Form):
    """Account and personal details an administrator can maintain for their own login."""
    username = forms.CharField(
        label='Username', max_length=30,
        help_text='3 to 30 characters: letters, numbers, dots, dashes or underscores.',
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'username', 'maxlength': 30}),
    )
    first_name = forms.CharField(
        label='First name', max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'given-name'}),
    )
    middle_name = forms.CharField(
        label='Middle name', max_length=100, required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'additional-name'}),
    )
    last_name = forms.CharField(
        label='Last name', max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'family-name'}),
    )
    phone = forms.CharField(
        label='Contact number', required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'autocomplete': 'tel', 'inputmode': 'numeric', 'placeholder': '09XXXXXXXXX',
            'maxlength': '11', 'pattern': '09[0-9]{9}',
            'title': 'Enter an 11-digit Philippine mobile number starting with 09.',
        }),
    )
    email = forms.EmailField(
        label='Email', max_length=254,
        widget=forms.EmailInput(attrs={'class': 'form-control', 'autocomplete': 'email'}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = user

    def clean_username(self):
        return clean_new_username(self.cleaned_data.get('username'), self.account)

    def clean_first_name(self):
        return self.cleaned_data['first_name'].strip()

    def clean_middle_name(self):
        return (self.cleaned_data.get('middle_name') or '').strip()

    def clean_last_name(self):
        return self.cleaned_data['last_name'].strip()

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()
        if not phone:
            return ''
        if not phone.isascii() or not phone.isdigit():
            raise forms.ValidationError('Contact number must contain digits only.')
        if len(phone) != 11 or not phone.startswith('09'):
            raise forms.ValidationError('Enter a valid Philippine mobile number (09XXXXXXXXX).')
        return phone

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        taken = User.objects.filter(email__iexact=email)
        if self.account is not None:
            taken = taken.exclude(pk=self.account.pk)
        if taken.exists():
            raise forms.ValidationError('That email is already used by another account.')
        return email
