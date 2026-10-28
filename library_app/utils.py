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

from .models import (
    UserProfile, Member, Book, BorrowTransaction,
    Reservation, FinePayment, Notification, AuditLog
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
    today = timezone.now().date()
    two_days_later = today + timedelta(days=2)

    # 1. Overdue transactions
    overdue_txns = BorrowTransaction.objects.filter(status='BORROWED', due_date__lt=today)
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
                    'message': f"Your borrowed book '{txn.book.title}' is overdue by {days} day(s). Accumulated fine: ${txn.fine_amount:.2f}. Please return it immediately."
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
    if overdue_txns.exists():
        librarians = User.objects.filter(profile__role__in=['ADMIN', 'LIBRARIAN'])
        for lib in librarians:
            Notification.objects.get_or_create(
                recipient=lib,
                notification_type='LIBRARIAN_ALERT',
                title="System Alert: Overdue Books Detected",
                defaults={
                    'message': f"There are currently {overdue_txns.count()} unreturned book(s) past their due dates. Review the overdue reports."
                }
            )


def notify_reservation_ready(book):
    """When a book is returned, fulfill the next pending reservation and alert the member."""
    next_reservation = book.reservations.filter(status='PENDING').order_by('reservation_date').first()
    if next_reservation:
        next_reservation.status = 'READY'
        next_reservation.notified_at = timezone.now()
        next_reservation.save(update_fields=['status', 'notified_at'])
        book.status = 'RESERVED'
        book.save(update_fields=['status'])

        if next_reservation.member.user:
            Notification.objects.create(
                recipient=next_reservation.member.user,
                notification_type='RESERVATION_READY',
                title=f"Reserved Book Ready: '{book.title}'",
                message=f"Good news! '{book.title}' is now ready for you to pick up at shelf #{book.shelf_number}."
            )
        return next_reservation
    else:
        book.update_availability_status()
        return None


# --- PDF Receipt Generator ---
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
        ["Amount Paid:", f"${payment.amount_paid:.2f}"],
        ["Remaining Balance:", f"${payment.member.balance:.2f}"],
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
def export_report_excel(report_type, query_data, title):
    wb = Workbook()
    ws = wb.active
    ws.title = title[:30]

    # Header style
    header_font = Font(name='Arial', size=11, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # Title Banner
    ws.merge_cells('A1:F1')
    ws['A1'] = f"Library Management System - {title}"
    ws['A1'].font = Font(name='Arial', size=14, bold=True, color='1E293B')
    ws['A1'].alignment = Alignment(horizontal='center')
    ws.append([])

    if report_type == 'borrowed_overdue':
        headers = ['Txn Code', 'Book Title', 'Member Name', 'Issue Date', 'Due Date', 'Status', 'Fine']
        ws.append(headers)
        for row in query_data:
            ws.append([
                row.transaction_code,
                row.book.title,
                row.member.full_name,
                row.issue_date.strftime("%Y-%m-%d"),
                row.due_date.strftime("%Y-%m-%d"),
                row.get_status_display(),
                f"${row.fine_amount:.2f}"
            ])
    elif report_type == 'most_borrowed':
        headers = ['Book Title', 'Author', 'Category', 'ISBN', 'Total Times Borrowed']
        ws.append(headers)
        for row in query_data:
            ws.append([
                row['title'],
                row['author'],
                row['category__name'],
                row['isbn'],
                row['borrow_count']
            ])
    elif report_type == 'members_status':
        headers = ['Member ID', 'Full Name', 'Email', 'Phone', 'Status', 'Account Balance', 'Joined']
        ws.append(headers)
        for row in query_data:
            ws.append([
                row.member_id,
                row.full_name,
                row.email,
                row.phone,
                row.get_status_display(),
                f"${row.balance:.2f}",
                row.created_at.strftime("%Y-%m-%d")
            ])
    elif report_type == 'transactions_summary':
        headers = ['Txn Code', 'Book Title', 'Member', 'Issued By', 'Issue Date', 'Return Date', 'Status']
        ws.append(headers)
        for row in query_data:
            ws.append([
                row.transaction_code,
                row.book.title,
                row.member.full_name,
                row.issued_by.username,
                row.issue_date.strftime("%Y-%m-%d"),
                row.return_date.strftime("%Y-%m-%d") if row.return_date else "Not Returned",
                row.get_status_display()
            ])

    # Format header row
    header_row_idx = 3
    for col in range(1, ws.max_column + 1):
        cell = ws.cell(row=header_row_idx, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
        ws.column_dimensions[cell.column_letter].width = 22

    # Border for data
    for row in ws.iter_rows(min_row=header_row_idx, max_row=ws.max_row, min_col=1, max_col=ws.max_column):
        for cell in row:
            cell.border = thin_border

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def export_report_pdf(report_type, query_data, title):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=16,
        leading=20,
        textColor=colors.HexColor('#0f172a'),
        alignment=1
    )
    meta_style = ParagraphStyle(
        'MetaStyle',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#64748b'),
        alignment=1
    )

    elements = [
        Paragraph("<b>LIBRARY MANAGEMENT SYSTEM</b>", title_style),
        Paragraph(f"{title} | Generated: {timezone.now().strftime('%Y-%m-%d %H:%M')}", meta_style),
        Spacer(1, 15)
    ]

    table_data = []
    if report_type == 'borrowed_overdue':
        table_data.append(['Txn Code', 'Book Title', 'Member', 'Due Date', 'Status', 'Fine'])
        for r in query_data[:40]:  # Limit page size cleanly
            table_data.append([
                r.transaction_code,
                r.book.title[:25],
                r.member.full_name[:20],
                r.due_date.strftime("%Y-%m-%d"),
                r.get_status_display(),
                f"${r.fine_amount:.2f}"
            ])
    elif report_type == 'most_borrowed':
        table_data.append(['Book Title', 'Author', 'Category', 'ISBN', 'Borrows'])
        for r in query_data[:40]:
            table_data.append([
                r['title'][:30],
                r['author'][:20],
                r['category__name'][:15],
                r['isbn'],
                str(r['borrow_count'])
            ])
    elif report_type == 'members_status':
        table_data.append(['Member ID', 'Name', 'Email', 'Phone', 'Status', 'Balance'])
        for r in query_data[:40]:
            table_data.append([
                r.member_id,
                r.full_name[:20],
                r.email[:25],
                r.phone,
                r.get_status_display(),
                f"${r.balance:.2f}"
            ])
    elif report_type == 'transactions_summary':
        table_data.append(['Code', 'Book', 'Member', 'Issued', 'Returned', 'Status'])
        for r in query_data[:40]:
            ret_str = r.return_date.strftime("%Y-%m-%d") if r.return_date else "-"
            table_data.append([
                r.transaction_code,
                r.book.title[:22],
                r.member.full_name[:18],
                r.issue_date.strftime("%Y-%m-%d"),
                ret_str,
                r.get_status_display()
            ])

    if table_data:
        t = Table(table_data)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')])
        ]))
        elements.append(t)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# --- Database Backup & Restore Utility ---
def create_database_backup():
    """Exports all key models to JSON format."""
    data = {}
    models_to_backup = [UserProfile, Category, Member, Book, BorrowTransaction, Reservation, FinePayment, AuditLog]
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
