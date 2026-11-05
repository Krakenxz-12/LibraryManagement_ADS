from library_app.models import Category
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from django.core.serializers import serialize, deserialize
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import Count, Sum

from .services import expire_stale_holds
from .models import (
    UserProfile, Member, Book, BorrowTransaction, BorrowingTier,
    Reservation, FinePayment, Notification, AuditLog, ACTIVE_LOAN_STATUSES
)

# --- Role Checking Decorators ---
def role_required(*allowed_roles):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.shortcuts import redirect
                return redirect('login')
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            profile = getattr(request.user, 'profile', None)
            if profile and profile.role in allowed_roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied("You do not have permission to access this resource.")
        return _wrapped_view
    return decorator

librarian_required = role_required('ADMIN', 'LIBRARIAN')
admin_required = role_required('ADMIN')


# --- Audit Logging Helper ---
def log_audit(request, action, model_name, object_id='', details=''):
    ip = request.META.get('REMOTE_ADDR', '') if request else ''
    user = request.user if request and request.user.is_authenticated else None
    AuditLog.objects.create(
        user=user,
        action=action,
        model_name=model_name,
        object_id=str(object_id),
        details=details,
        ip_address=ip
    )


# --- System Notifications & Automated Alerts ---
def check_and_create_alerts():
    """
    Scans transactions and reservations to dispatch alerts:
    - Overdue alerts to members
    - Upcoming due date reminders (within 2 days)
    - Alerts to librarians for overdue/unreturned books
    """
    expire_stale_holds()  # release uncollected holds first

    today = timezone.localdate()
    two_days_later = today + timedelta(days=2)

    # 1. Overdue transactions
    # Evaluate once (list) so the later "any overdue?" check isn't affected by the status update below
    overdue_txns = list(
        BorrowTransaction.objects.filter(status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today)
        .select_related('member__user', 'book')
    )
    for txn in overdue_txns:
        txn.status = 'OVERDUE'
        txn.fine_amount = txn.compute_fine()
        txn.save(update_fields=['status', 'fine_amount'])

        # Notify member
        if txn.member.user:
            days = (today - txn.due_date).days
            Notification.objects.get_or_create(
                recipient=txn.member.user,
                notification_type='OVERDUE',
                title=f"Overdue Alert: '{txn.book.title}'",
                defaults={
                    'message': f"Your borrowed book '{txn.book.title}' is overdue by {days} day(s). Accumulated fine: ₱{txn.fine_amount:.2f}. Please return it immediately."
                }
            )

    # 2. Upcoming due date warnings (within 2 days)
    upcoming_txns = BorrowTransaction.objects.filter(
        status='BORROWED',
        due_date__gte=today,
        due_date__lte=two_days_later
    )
    for txn in upcoming_txns:
        if txn.member.user:
            Notification.objects.get_or_create(
                recipient=txn.member.user,
                notification_type='DUE_DATE',
                title=f"Due Date Reminder: '{txn.book.title}'",
                defaults={
                    'message': f"Friendly reminder: '{txn.book.title}' is due for return on {txn.due_date.strftime('%b %d, %Y')}."
                }
            )

    # 3. Librarian alerts for overdue/unreturned books
    if overdue_txns:
        librarians = User.objects.filter(profile__role__in=['ADMIN', 'LIBRARIAN'])
        for lib in librarians:
            Notification.objects.get_or_create(
                recipient=lib,
                notification_type='LIBRARIAN_ALERT',
                title="System Alert: Overdue Books Detected",
                defaults={
                    'message': f"There are currently {len(overdue_txns)} unreturned book(s) past their due dates. Review the overdue reports."
                }
            )


def generate_receipt_pdf(payment):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'ReceiptTitle',
        parent=styles['Heading1'],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1e293b'),
        alignment=1  # Center
    )
    subtitle_style = ParagraphStyle(
        'ReceiptSubtitle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#64748b'),
        alignment=1
    )
    bold_style = ParagraphStyle('BoldStyle', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#0f172a'))

    elements = []
    elements.append(Paragraph("<b>LIBRARY MANAGEMENT SYSTEM</b>", title_style))
    elements.append(Paragraph("Official Fine Payment Receipt", subtitle_style))
    elements.append(Spacer(1, 15))

    data = [
        ["Receipt Number:", payment.receipt_number],
        ["Transaction Ref:", payment.transaction.transaction_code],
        ["Date & Time:", payment.payment_date.strftime("%Y-%m-%d %H:%M:%S")],
        ["Member ID:", payment.member.member_id],
        ["Member Name:", payment.member.full_name],
        ["Book Title:", payment.transaction.book.title],
        ["Payment Method:", payment.get_payment_method_display()],
        ["Collected By:", payment.collected_by.get_full_name() or payment.collected_by.username],
        ["Amount Paid:", f"₱{payment.amount_paid:.2f}"],
        ["Remaining Balance:", f"₱{payment.member.balance:.2f}"],
    ]

    table = Table(data, colWidths=[160, 320])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#475569')),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0, 8), (-1, 8), colors.HexColor('#e0e7ff')),
        ('TEXTCOLOR', (0, 8), (-1, 8), colors.HexColor('#1e1b4b')),
    ]))
    elements.append(table)

    elements.append(Spacer(1, 20))
    elements.append(Paragraph("Thank you for your payment. Please keep this receipt for your records.", subtitle_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# --- PDF & Excel Reports Generator ---
def create_database_backup():
    """Exports all key models to JSON format."""
    data = {}
    models_to_backup = [UserProfile, BorrowingTier, Category, Member, Book, BorrowTransaction, Reservation, FinePayment, AuditLog]
    for model in models_to_backup:
        serialized = serialize('json', model.objects.all())
        data[model.__name__] = json.loads(serialized)
    return json.dumps(data, indent=2)


def restore_database_backup(json_content):
    """Restores database from serialized JSON content."""
    data = json.loads(json_content)
    restored_count = 0
    # In reverse dependency order or deserialization
    for model_name, records in data.items():
        for obj in deserialize('json', json.dumps(records)):
            obj.save()
            restored_count += 1
    return restored_count
