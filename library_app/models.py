from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

# A loan that has not been returned yet (status flips to OVERDUE automatically)
ACTIVE_LOAN_STATUSES = ['BORROWED', 'OVERDUE']


class LibrarySettings(models.Model):
    """Library rules edited by the Admin. One row only (pk=1), created on first use.
    Changes apply to NEW loans/calculations; existing loans keep their due date and fine rate."""
    daily_fine_rate = models.DecimalField(
        'Fine per overdue day (PHP)', max_digits=8, decimal_places=2, default=Decimal('20.00'),
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('10000.00'))],
    )
    weekly_borrow_limit = models.PositiveSmallIntegerField(
        'Books a member may collect per week', default=5,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
    )
    max_renewals = models.PositiveSmallIntegerField(
        'Renewals allowed per loan', default=1,
        validators=[MaxValueValidator(5)],
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        verbose_name_plural = 'Library settings'

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            'daily_fine_rate': Decimal(str(getattr(settings, 'DAILY_FINE_RATE', '20.00'))),
            'weekly_borrow_limit': getattr(settings, 'WEEKLY_BORROW_LIMIT', 5),
            'max_renewals': getattr(settings, 'MAX_RENEWALS', 1),
        })
        return obj

    def __str__(self):
        return "Library settings"


class BorrowingTier(models.Model):
    """A named loan length (e.g. Short-term = 7 days). Books and categories point at a tier.
    Editing a tier only affects NEW loans: every loan stores its own due date and day count."""
    name = models.CharField(max_length=50, unique=True)
    days = models.PositiveSmallIntegerField(
        'Borrowing duration (days)', validators=[MinValueValidator(1), MaxValueValidator(365)],
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)  # used when neither the book nor its category names a tier
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['days', 'name']

    def __str__(self):
        return f"{self.name} ({self.days} days)"

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_default=True, is_active=True).first()


class LoanTerms:
    """Result of working out how long a book may be borrowed, and where that rule came from."""
    __slots__ = ('days', 'name', 'tier', 'source')

    SOURCE_LABELS = {'book': 'set on this book', 'category': 'category default',
                     'default': 'library default', 'fallback': 'built-in default'}

    def __init__(self, days, name, tier, source):
        self.days, self.name, self.tier, self.source = days, name, tier, source

    @property
    def source_label(self):
        return self.SOURCE_LABELS[self.source]

    def __str__(self):
        return f"{self.name} ({self.days} days)"


class UserProfile(models.Model):
    ROLE_CHOICES = [
        ('ADMIN', 'Administrator'),
        ('LIBRARIAN', 'Librarian'),
        ('MEMBER', 'Member'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='MEMBER')
    phone = models.CharField(max_length=20, blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return self.role == 'ADMIN' or self.user.is_superuser

    @property
    def is_librarian(self):
        return self.role in ['ADMIN', 'LIBRARIAN'] or self.user.is_superuser

    @property
    def is_member(self):
        return self.role == 'MEMBER'


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    borrowing_tier = models.ForeignKey(
        BorrowingTier, on_delete=models.SET_NULL, null=True, blank=True, related_name='categories',
        help_text='Default loan period for books in this category. Empty = library default.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class Member(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
    ]

    MEMBERSHIP_TYPE_CHOICES = [
        ('STUDENT', 'Student'),
        ('FACULTY', 'Faculty'),
        ('STAFF', 'Staff'),
        ('COMMUNITY', 'Community / External'),
    ]

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='member_account')
    member_id = models.CharField(max_length=20, unique=True, db_index=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20)
    address = models.TextField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    membership_type = models.CharField(max_length=15, choices=MEMBERSHIP_TYPE_CHOICES, default='STUDENT')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='ACTIVE')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.member_id} - {self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def active_borrows_count(self):
        return self.borrows.filter(status__in=ACTIVE_LOAN_STATUSES).count()

    @property
    def overdue_borrows_count(self):
        today = timezone.localdate()
        return self.borrows.filter(status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today).count()

    @classmethod
    def generate_member_id(cls):
        year = timezone.now().year
        last_member = cls.objects.filter(member_id__startswith=f"MEM-{year}-").order_by('-id').first()
        if last_member:
            try:
                seq = int(last_member.member_id.split('-')[-1]) + 1
            except (ValueError, IndexError):
                seq = cls.objects.count() + 1
        else:
            seq = 1
        return f"MEM-{year}-{seq:04d}"


class Book(models.Model):
    STATUS_CHOICES = [
        ('AVAILABLE', 'Available'),
        ('BORROWED', 'Borrowed'),
        ('RESERVED', 'Reserved'),
        ('LOST', 'Lost'),
        ('UNDER_REPAIR', 'Under Repair'),
    ]

    title = models.CharField(max_length=255, db_index=True)
    author = models.CharField(max_length=255, db_index=True)
    isbn = models.CharField(max_length=20, unique=True, db_index=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='books')
    shelf_number = models.CharField(max_length=50)
    publication_year = models.PositiveIntegerField(null=True, blank=True)
    total_copies = models.PositiveIntegerField(default=1)
    available_copies = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='AVAILABLE')
    borrowing_tier = models.ForeignKey(
        BorrowingTier, on_delete=models.SET_NULL, null=True, blank=True, related_name='books',
        help_text="Overrides the category's loan period for this book. Empty = use the category default.",
    )
    cover_image_url = models.CharField(max_length=500, blank=True, default='')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def cover_url(self):
        if self.cover_image_url:
            return self.cover_image_url
        clean_isbn = self.isbn.replace('-', '').strip()
        if clean_isbn:
            return f"https://covers.openlibrary.org/b/isbn/{clean_isbn}-M.jpg"
        return "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?auto=format&fit=crop&w=400&q=80"

    def resolve_loan_terms(self):
        """Loan period for this book: its own tier, else its category's, else the library default.
        Inactive tiers are skipped. Returns a LoanTerms."""
        for source, tier in (('book', self.borrowing_tier), ('category', self.category.borrowing_tier)):
            if tier and tier.is_active:
                return LoanTerms(tier.days, tier.name, tier, source)
        tier = BorrowingTier.get_default()
        if tier:
            return LoanTerms(tier.days, tier.name, tier, 'default')
        return LoanTerms(getattr(settings, 'BORROW_PERIOD_DAYS', 14), 'Standard', None, 'fallback')

    @property
    def availability_percent(self):
        if self.total_copies > 0:
            return int((self.available_copies / self.total_copies) * 100)
        return 0


    class Meta:
        ordering = ['title']

    def __str__(self):
        return f"{self.title} by {self.author} ({self.isbn})"

    def update_availability_status(self):
        """Recompute status from stock/reservations and save status AND stock count."""
        if self.status not in ['LOST', 'UNDER_REPAIR']:
            if self.available_copies > 0:
                self.status = 'AVAILABLE'
            else:
                # No free copy: it is either on loan, or being held for someone to pick up
                waiting = (
                    self.reservations.filter(status__in=['PENDING', 'READY']).exists()
                    or self.borrow_requests.filter(status='APPROVED').exists()
                )
                self.status = 'RESERVED' if waiting else 'BORROWED'
        self.save(update_fields=['status', 'available_copies'])


class BorrowTransaction(models.Model):
    STATUS_CHOICES = [
        ('BORROWED', 'Borrowed'),
        ('RETURNED', 'Returned'),
        ('OVERDUE', 'Overdue'),
    ]

    transaction_code = models.CharField(max_length=30, unique=True, db_index=True)
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='borrows')
    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name='borrows')
    issued_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='issued_transactions')
    returned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='received_transactions')
    issue_date = models.DateField(default=timezone.now)
    due_date = models.DateField()
    return_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='BORROWED')
    fine_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'))
    daily_fine_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('20.00'))  # rate in force when the loan was created
    loan_days = models.PositiveSmallIntegerField(default=14)  # length of the loan (and of each renewal), fixed at checkout
    tier_name = models.CharField(max_length=50, blank=True)  # borrowing tier in force at checkout
    renewal_count = models.PositiveSmallIntegerField(default=0)
    fine_paid = models.BooleanField(default=False)
    reading_progress = models.PositiveIntegerField(default=0)  # e.g. 6% or 0% reading completion
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-issue_date', '-id']

    def __str__(self):
        return f"{self.transaction_code}: {self.book.title} -> {self.member.full_name}"

    @property
    def is_currently_overdue(self):
        today = timezone.localdate()
        if self.status in ACTIVE_LOAN_STATUSES and self.due_date < today:
            return True
        return False

    @property
    def days_overdue(self):
        today = timezone.localdate()
        comp_date = self.return_date if self.return_date else today
        if comp_date > self.due_date:
            return (comp_date - self.due_date).days
        return 0

    def compute_fine(self, daily_rate=None):
        if daily_rate is None:
            daily_rate = self.daily_fine_rate  # snapshot taken at checkout, so later rate changes don't alter it
        overdue_days = self.days_overdue
        if overdue_days > 0:
            return Decimal(str(overdue_days)) * daily_rate
        return Decimal('0.00')

    @classmethod
    def generate_transaction_code(cls):
        today_str = timezone.now().strftime("%Y%m%d")
        count = cls.objects.filter(transaction_code__startswith=f"TXN-{today_str}-").count() + 1
        return f"TXN-{today_str}-{count:04d}"


class Reservation(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('READY', 'Ready for Pickup'),
        ('FULFILLED', 'Fulfilled'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    ]

    reservation_code = models.CharField(max_length=30, unique=True, blank=True)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name='reservations')
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='reservations')
    reservation_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    notified_at = models.DateTimeField(null=True, blank=True)
    pickup_deadline = models.DateTimeField(null=True, blank=True)  # set when the copy is held (READY)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['reservation_date']

    def save(self, *args, **kwargs):
        if not self.reservation_code:
            year = timezone.now().year
            count = Reservation.objects.count() + 1
            self.reservation_code = f"RES-{year}-{count:04d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reservation_code}: {self.book.title} reserved by {self.member.full_name} ({self.status})"


class BorrowRequest(models.Model):
    """Member asks to borrow an available book.
    PENDING -> APPROVED (copy held, 24h to collect) -> COLLECTED (loan starts)
    or REJECTED / EXPIRED / CANCELLED."""
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Awaiting Pickup'),
        ('COLLECTED', 'Borrowed'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
    ]

    request_code = models.CharField(max_length=30, unique=True, null=True, blank=True)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name='borrow_requests')
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='borrow_requests')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    handled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='handled_borrow_requests')
    handled_at = models.DateTimeField(null=True, blank=True)
    pickup_deadline = models.DateTimeField(null=True, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(blank=True)
    transaction = models.OneToOneField(BorrowTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='source_request')

    class Meta:
        ordering = ['-requested_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.request_code:
            self.request_code = f"REQ-{timezone.localdate().year}-{self.pk:04d}"
            BorrowRequest.objects.filter(pk=self.pk).update(request_code=self.request_code)

    def __str__(self):
        return f"{self.request_code}: {self.book.title} requested by {self.member.full_name} ({self.status})"


class FinePayment(models.Model):
    PAYMENT_METHODS = [
        ('CASH', 'Cash'),
        ('GCASH', 'GCash'),
        ('MAYA', 'Maya / PayMaya'),
        ('CARD', 'Credit / Debit Card'),
        ('ONLINE', 'Online Bank Transfer'),
    ]

    receipt_number = models.CharField(max_length=30, unique=True, db_index=True)
    transaction = models.ForeignKey(BorrowTransaction, on_delete=models.PROTECT, related_name='payments')
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='payments')
    amount_paid = models.DecimalField(max_digits=8, decimal_places=2)
    payment_date = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='CASH')
    collected_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='collected_payments')
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-payment_date']

    def __str__(self):
        return f"{self.receipt_number} - ₱{self.amount_paid} ({self.member.full_name})"

    @classmethod
    def generate_receipt_number(cls):
        date_str = timezone.now().strftime("%Y%m%d")
        count = cls.objects.filter(receipt_number__startswith=f"REC-{date_str}-").count() + 1
        return f"REC-{date_str}-{count:04d}"


class Notification(models.Model):
    TYPE_CHOICES = [
        ('DUE_DATE', 'Due Date Reminder'),
        ('OVERDUE', 'Overdue Alert'),
        ('RESERVATION_READY', 'Reservation Ready'),
        ('LIBRARIAN_ALERT', 'Librarian System Alert'),
        ('PAYMENT', 'Payment Confirmation'),
        ('GENERAL', 'General Announcement'),
    ]

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='GENERAL')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.notification_type}] {self.title} -> {self.recipient.username}"


class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    model_name = models.CharField(max_length=50)
    object_id = models.CharField(max_length=50, blank=True)
    details = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.timestamp.strftime('%Y-%m-%d %H:%M')} | {self.user} | {self.action} on {self.model_name}"
