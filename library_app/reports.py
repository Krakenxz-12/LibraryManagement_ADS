"""Admin reports: every figure comes from live database records.

build_report() returns {'title', 'subtitle', 'headers', 'rows'} so the same data feeds the
on-screen tables and the PDF / Excel exports.
"""
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Count, DecimalField, Sum, Value, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    ACTIVE_LOAN_STATUSES, Book, BorrowRequest, BorrowTransaction, Category, FinePayment,
    Member, Reservation,
)

ZERO = Decimal('0.00')

# key, title, description
REPORT_TYPES = [
    ('overview', 'Library Overview', 'Totals: books, members, librarians, loans, overdue, requests, reservations and fines.'),
    ('borrowing_activity', 'Borrowing Activity', 'Every loan issued within the selected date range.'),
    ('most_borrowed', 'Most Borrowed Books', 'Books ranked by how often they were borrowed in the date range.'),
    ('active_borrowers', 'Most Active Borrowers', 'Members ranked by loans in the date range.'),
    ('category_stats', 'Book Category Statistics', 'Titles, copies and borrowing activity per category.'),
    ('fines_collected', 'Fines Collected', 'Fine payments received within the date range.'),
    ('fines_outstanding', 'Outstanding Fines', 'Fines that have not been fully paid yet.'),
    ('borrowed_overdue', 'Borrowed & Overdue Books', 'Books currently on loan, including overdue ones.'),
    ('members_status', 'Members & Balances', 'All registered members with type, status and balance.'),
]
REPORT_TITLES = {k: t for k, t, _ in REPORT_TYPES}


# ------------------------------------------------------------------ date range
def parse_range(params):
    """(start, end, note). Defaults to the last 30 days; swaps the dates if they are reversed."""
    today = timezone.localdate()
    note = ''

    def read(name, default):
        raw = (params.get(name) or '').strip()
        if not raw:
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return default

    start = read('start', today - timedelta(days=29))
    end = read('end', today)
    if start > end:
        start, end = end, start
        note = 'The start date was after the end date, so the dates were swapped.'
    return start, end, note


def _bounds(start, end):
    """Timezone-aware [start, end+1day) bounds. (Avoids __date lookups, which need MySQL timezone tables.)"""
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))


def _money(value):
    return f"PHP {Decimal(value or 0):,.2f}"


# ------------------------------------------------------------------ building blocks
def outstanding_fine_rows():
    """Unpaid fines: list of dicts with fine, paid and outstanding amounts."""
    qs = (
        BorrowTransaction.objects.filter(fine_amount__gt=0)
        .annotate(paid=Coalesce(Sum('payments__amount_paid'), Value(ZERO), output_field=DecimalField(max_digits=10, decimal_places=2)))
        .select_related('member', 'book').order_by('member__last_name', 'due_date')
    )
    rows = []
    for t in qs:
        outstanding = t.fine_amount - t.paid
        if outstanding > 0:
            rows.append({'txn': t, 'fine': t.fine_amount, 'paid': t.paid, 'outstanding': outstanding})
    return rows


def overview_stats(start, end):
    today = timezone.localdate()
    s_dt, e_dt = _bounds(start, end)
    books = Book.objects.aggregate(copies=Sum('total_copies'), available=Sum('available_copies'))
    collected = FinePayment.objects.filter(payment_date__gte=s_dt, payment_date__lt=e_dt).aggregate(t=Sum('amount_paid'))['t'] or ZERO
    outstanding = sum((r['outstanding'] for r in outstanding_fine_rows()), ZERO)
    return {
        'total_titles': Book.objects.count(),
        'total_copies': books['copies'] or 0,
        'available_copies': books['available'] or 0,
        'total_members': Member.objects.count(),
        'active_members': Member.objects.filter(status='ACTIVE').count(),
        'active_librarians': User.objects.filter(profile__role='LIBRARIAN', is_active=True).count(),
        'currently_borrowed': BorrowTransaction.objects.filter(status__in=ACTIVE_LOAN_STATUSES).count(),
        'overdue': BorrowTransaction.objects.filter(status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today).count(),
        'pending_requests': BorrowRequest.objects.filter(status='PENDING').count(),
        'active_reservations': Reservation.objects.filter(status__in=['PENDING', 'READY']).count(),
        'fines_collected': collected,
        'fines_outstanding': outstanding,
    }


def top_books(start, end, limit=None):
    qs = (Book.objects.select_related('category')
          .annotate(n=Count('borrows', filter=Q(borrows__issue_date__range=(start, end))))
          .filter(n__gt=0).order_by('-n', 'title'))
    return qs[:limit] if limit else qs


def top_borrowers(start, end, limit=None):
    qs = (Member.objects
          .annotate(n=Count('borrows', filter=Q(borrows__issue_date__range=(start, end))))
          .filter(n__gt=0).order_by('-n', 'last_name'))
    return qs[:limit] if limit else qs


def category_rows(start, end):
    stats = {
        r['category__name']: r for r in
        Book.objects.values('category__name').annotate(titles=Count('id'), copies=Sum('total_copies'))
    }
    borrows = dict(
        BorrowTransaction.objects.filter(issue_date__range=(start, end))
        .values_list('book__category__name').annotate(n=Count('id'))
    )
    rows = []
    for c in Category.objects.order_by('name'):
        s = stats.get(c.name, {})
        rows.append({'name': c.name, 'titles': s.get('titles', 0), 'copies': s.get('copies') or 0, 'borrows': borrows.get(c.name, 0)})
    return rows


# ------------------------------------------------------------------ report builder
def build_report(report_type, start, end):
    if report_type not in REPORT_TITLES:
        raise KeyError(report_type)
    title = REPORT_TITLES[report_type]
    ranged = report_type in ('overview', 'borrowing_activity', 'most_borrowed', 'active_borrowers',
                             'category_stats', 'fines_collected')
    subtitle = f"Period: {start:%b %d, %Y} to {end:%b %d, %Y}" if ranged else f"As of {timezone.localdate():%b %d, %Y}"
    today = timezone.localdate()

    if report_type == 'overview':
        s = overview_stats(start, end)
        headers = ['Metric', 'Value']
        rows = [
            ['Total book titles', s['total_titles']], ['Total book copies', s['total_copies']],
            ['Available copies', s['available_copies']], ['Currently borrowed', s['currently_borrowed']],
            ['Overdue books', s['overdue']], ['Registered members', s['total_members']],
            ['Active members', s['active_members']], ['Active librarians', s['active_librarians']],
            ['Pending borrow requests', s['pending_requests']], ['Active reservations', s['active_reservations']],
            ['Fines collected (period)', _money(s['fines_collected'])], ['Fines outstanding (now)', _money(s['fines_outstanding'])],
        ]
    elif report_type == 'borrowing_activity':
        headers = ['Txn Code', 'Book', 'Member', 'Issued By', 'Issued', 'Due', 'Returned', 'Status']
        qs = (BorrowTransaction.objects.filter(issue_date__range=(start, end))
              .select_related('book', 'member', 'issued_by').order_by('-issue_date', '-id'))
        rows = [[t.transaction_code, t.book.title, t.member.full_name, t.issued_by.get_full_name() or t.issued_by.username,
                 t.issue_date.isoformat(), t.due_date.isoformat(), t.return_date.isoformat() if t.return_date else '-',
                 t.get_status_display()] for t in qs]
    elif report_type == 'most_borrowed':
        headers = ['Rank', 'Title', 'Author', 'Category', 'Times Borrowed']
        rows = [[i, b.title, b.author, b.category.name, b.n] for i, b in enumerate(top_books(start, end, 50), 1)]
    elif report_type == 'active_borrowers':
        headers = ['Rank', 'Member ID', 'Name', 'Type', 'Books Borrowed']
        rows = [[i, m.member_id, m.full_name, m.get_membership_type_display(), m.n] for i, m in enumerate(top_borrowers(start, end, 50), 1)]
    elif report_type == 'category_stats':
        headers = ['Category', 'Titles', 'Copies', 'Borrowed in Period']
        rows = [[r['name'], r['titles'], r['copies'], r['borrows']] for r in category_rows(start, end)]
    elif report_type == 'fines_collected':
        headers = ['Receipt No.', 'Date', 'Member', 'Book', 'Method', 'Amount']
        s_dt, e_dt = _bounds(start, end)
        pays = (FinePayment.objects.filter(payment_date__gte=s_dt, payment_date__lt=e_dt)
                .select_related('member', 'transaction__book').order_by('-payment_date'))
        rows = [[p.receipt_number, timezone.localtime(p.payment_date).strftime('%Y-%m-%d %H:%M'), p.member.full_name,
                 p.transaction.book.title, p.get_payment_method_display(), _money(p.amount_paid)] for p in pays]
        rows.append(['TOTAL', '', '', '', '', _money(sum((p.amount_paid for p in pays), ZERO))])
    elif report_type == 'fines_outstanding':
        headers = ['Member ID', 'Member', 'Book', 'Due Date', 'Days Late', 'Fine', 'Paid', 'Outstanding']
        data = outstanding_fine_rows()
        rows = [[r['txn'].member.member_id, r['txn'].member.full_name, r['txn'].book.title, r['txn'].due_date.isoformat(),
                 r['txn'].days_overdue, _money(r['fine']), _money(r['paid']), _money(r['outstanding'])] for r in data]
        rows.append(['TOTAL', '', '', '', '', '', '', _money(sum((r['outstanding'] for r in data), ZERO))])
    elif report_type == 'borrowed_overdue':
        headers = ['Txn Code', 'Book', 'Member', 'Issued', 'Due', 'Days Overdue', 'Status', 'Fine So Far']
        qs = (BorrowTransaction.objects.filter(status__in=ACTIVE_LOAN_STATUSES)
              .select_related('book', 'member').order_by('due_date'))
        rows = [[t.transaction_code, t.book.title, t.member.full_name, t.issue_date.isoformat(), t.due_date.isoformat(),
                 t.days_overdue, 'Overdue' if t.due_date < today else 'On loan', _money(t.compute_fine())] for t in qs]
    else:  # members_status
        headers = ['Member ID', 'Name', 'Type', 'Email', 'Phone', 'Status', 'Balance', 'Joined']
        rows = [[m.member_id, m.full_name, m.get_membership_type_display(), m.email, m.phone, m.get_status_display(),
                 _money(m.balance), timezone.localtime(m.created_at).strftime('%Y-%m-%d')]
                for m in Member.objects.order_by('status', 'last_name')]

    return {'key': report_type, 'title': title, 'subtitle': subtitle, 'headers': headers, 'rows': rows}


# ------------------------------------------------------------------ exporters
def export_excel(report):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = report['title'][:31]
    ws['A1'] = report['title']
    ws['A1'].font = Font(name='Arial', size=14, bold=True)
    ws['A2'] = report['subtitle']
    ws['A2'].font = Font(name='Arial', size=10, italic=True, color='64748B')
    ws.append([])
    ws.append(report['headers'])
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(name='Arial', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='163832')
        cell.alignment = Alignment(horizontal='center', vertical='center')
    for row in report['rows']:
        ws.append(row)
    for i, header in enumerate(report['headers'], 1):
        width = max([len(str(header))] + [len(str(r[i - 1])) for r in report['rows'] if len(r) >= i] or [10])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 50)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    page = landscape(letter) if len(report['headers']) > 5 else letter
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=30, rightMargin=30, topMargin=36, bottomMargin=30,
                            title=report['title'])
    styles = getSampleStyleSheet()
    cell = ParagraphStyle('cell', parent=styles['Normal'], fontSize=8, leading=10)
    head = ParagraphStyle('head', parent=cell, textColor=colors.white, fontName='Helvetica-Bold')
    data = [[Paragraph(str(h), head) for h in report['headers']]]
    for row in report['rows']:
        data.append([Paragraph(str(v).replace('&', '&amp;').replace('<', '&lt;'), cell) for v in row])
    if not report['rows']:
        data.append([Paragraph('No records for this report.', cell)] + [''] * (len(report['headers']) - 1))
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#163832')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#CBD5E1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F1F5F9')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    doc.build([
        Paragraph(report['title'], styles['Title']),
        Paragraph(report['subtitle'], styles['Normal']),
        Spacer(1, 10), table,
    ])
    return buf.getvalue()
