from datetime import timedelta, date
from decimal import Decimal
import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.db.models import Q, Count, Sum
from django.utils import timezone

from .models import (
    UserProfile, Category, Member, Book, BorrowTransaction,
    Reservation, FinePayment, Notification, AuditLog
)
from .utils import (
    role_required, librarian_required, admin_required,
    log_audit, check_and_create_alerts, notify_reservation_ready,
    generate_receipt_pdf, export_report_excel, export_report_pdf,
    create_database_backup, restore_database_backup
)


# ==========================================
# AUTHENTICATION & DASHBOARD
# ==========================================

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            log_audit(request, 'LOGIN', 'User', user.id, f"User {username} logged in successfully.")
            messages.success(request, f"Welcome back, {user.first_name or user.username}!")
            return redirect('dashboard')
        else:
            messages.error(request, "Invalid username or password. Please try again.")

    return render(request, 'library_app/login.html')


@login_required
def logout_view(request):
    username = request.user.username
    log_audit(request, 'LOGOUT', 'User', request.user.id, f"User {username} logged out.")
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('login')


@login_required
def dashboard_view(request):
    # Auto-scan alerts on dashboard visit
    check_and_create_alerts()

    today = timezone.now().date()
    user = request.user
    role = getattr(user.profile, 'role', 'MEMBER') if hasattr(user, 'profile') else 'ADMIN'

    # Key statistics
    total_books = Book.objects.count()
    available_books = Book.objects.filter(status='AVAILABLE').count()
    borrowed_books = Book.objects.filter(status='BORROWED').count()
    overdue_books = BorrowTransaction.objects.filter(status='BORROWED', due_date__lt=today).count()
    total_members = Member.objects.count()
    active_members = Member.objects.filter(status='ACTIVE').count()
    unpaid_fines = BorrowTransaction.objects.filter(fine_amount__gt=0, fine_paid=False).aggregate(total=Sum('fine_amount'))['total'] or Decimal('0.00')

    # Recent transactions
    recent_transactions = BorrowTransaction.objects.select_related('book', 'member').all()[:6]

    # Overdue list
    overdue_txns = BorrowTransaction.objects.filter(
        status='BORROWED', due_date__lt=today
    ).select_related('book', 'member')[:5]

    # User notifications
    user_notifications = Notification.objects.filter(recipient=user)[:5]
    unread_notifications_count = Notification.objects.filter(recipient=user, is_read=False).count()

    # Member-specific data if role is MEMBER
    member_profile = None
    member_active_borrows = []
    member_reservations = []
    if role == 'MEMBER':
        member_profile = Member.objects.filter(user=user).first()
        if member_profile:
            member_active_borrows = BorrowTransaction.objects.filter(member=member_profile, status='BORROWED')
            member_reservations = Reservation.objects.filter(member=member_profile).exclude(status__in=['FULFILLED', 'CANCELLED'])

    # Recent list of books for visual carousel/grid
    recent_books = Book.objects.select_related('category').all().order_by('-created_at')[:10]

    context = {
        'total_books': total_books,
        'available_books': available_books,
        'borrowed_books': borrowed_books,
        'overdue_books': overdue_books,
        'total_members': total_members,
        'active_members': active_members,
        'unpaid_fines': unpaid_fines,
        'recent_transactions': recent_transactions,
        'overdue_txns': overdue_txns,
        'recent_books': recent_books,
        'user_notifications': user_notifications,
        'unread_notifications_count': unread_notifications_count,
        'role': role,
        'member_profile': member_profile,
        'member_active_borrows': member_active_borrows,
        'member_reservations': member_reservations,
    }
    return render(request, 'library_app/dashboard.html', context)


# ==========================================
# MODULE 1: MEMBER REGISTRATION & MANAGEMENT
# ==========================================

@login_required
@librarian_required
def member_list_view(request):
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()

    members = Member.objects.all()
    if query:
        members = members.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(member_id__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query)
        )
    if status_filter:
        members = members.filter(status=status_filter)

    categories = Category.objects.all()

    context = {
        'members': members,
        'query': query,
        'status_filter': status_filter,
    }
    return render(request, 'library_app/member_list.html', context)


@login_required
@librarian_required
def member_create_view(request):
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        dob = request.POST.get('date_of_birth') or None

        if Member.objects.filter(email=email).exists():
            messages.error(request, f"A member with email '{email}' already exists.")
            return redirect('member_list')

        member_id = Member.generate_member_id()

        # Create associated User account if username requested
        create_user_account = request.POST.get('create_user_account') == 'on'
        linked_user = None
        if create_user_account:
            username = request.POST.get('username', '').strip() or member_id.lower()
            password = request.POST.get('password', 'member123')
            if not User.objects.filter(username=username).exists():
                linked_user = User.objects.create_user(
                    username=username, email=email, password=password,
                    first_name=first_name, last_name=last_name
                )
                UserProfile.objects.create(user=linked_user, role='MEMBER', phone=phone)

        member = Member.objects.create(
            user=linked_user,
            member_id=member_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            address=address,
            date_of_birth=dob,
            status='ACTIVE'
        )

        log_audit(request, 'CREATE_MEMBER', 'Member', member.id, f"Registered member {member_id} ({member.full_name})")
        messages.success(request, f"Member '{member.full_name}' registered successfully with ID: {member_id}")
        return redirect('member_list')

    return redirect('member_list')


@login_required
@librarian_required
def member_edit_view(request, member_id):
    member = get_object_or_404(Member, id=member_id)

    if request.method == 'POST':
        member.first_name = request.POST.get('first_name', '').strip()
        member.last_name = request.POST.get('last_name', '').strip()
        member.email = request.POST.get('email', '').strip()
        member.phone = request.POST.get('phone', '').strip()
        member.address = request.POST.get('address', '').strip()
        dob = request.POST.get('date_of_birth')
        if dob:
            member.date_of_birth = dob
        member.status = request.POST.get('status', member.status)
        member.save()

        log_audit(request, 'UPDATE_MEMBER', 'Member', member.id, f"Updated member {member.member_id}")
        messages.success(request, f"Member '{member.full_name}' updated successfully.")
        return redirect('member_detail', member_id=member.id)

    return redirect('member_detail', member_id=member.id)


@login_required
@librarian_required
def member_toggle_status_view(request, member_id):
    member = get_object_or_404(Member, id=member_id)
    if member.status == 'ACTIVE':
        member.status = 'INACTIVE'
        msg = f"Member {member.member_id} deactivated."
    else:
        member.status = 'ACTIVE'
        msg = f"Member {member.member_id} activated."
    member.save()
    log_audit(request, 'TOGGLE_MEMBER_STATUS', 'Member', member.id, msg)
    messages.info(request, msg)
    return redirect('member_list')


@login_required
def member_detail_view(request, member_id):
    member = get_object_or_404(Member, id=member_id)

    # Permission check: Members can only see their own profile
    if hasattr(request.user, 'profile') and request.user.profile.is_member:
        if member.user != request.user:
            messages.error(request, "You can only view your own membership profile.")
            return redirect('dashboard')

    borrows = member.borrows.select_related('book').all()
    reservations = member.reservations.select_related('book').all()
    payments = member.payments.select_related('transaction', 'collected_by').all()

    context = {
        'member': member,
        'borrows': borrows,
        'reservations': reservations,
        'payments': payments,
    }
    return render(request, 'library_app/member_detail.html', context)


# ==========================================
# MODULE 2 & 8: BOOK CATALOG & SEARCH
# ==========================================

@login_required
def book_list_view(request):
    query = request.GET.get('q', '').strip()
    category_id = request.GET.get('category', '').strip()
    status_filter = request.GET.get('status', '').strip()
    sort_by = request.GET.get('sort', 'title')

    books = Book.objects.select_related('category').all()

    if query:
        books = books.filter(
            Q(title__icontains=query) |
            Q(author__icontains=query) |
            Q(isbn__icontains=query) |
            Q(shelf_number__icontains=query) |
            Q(category__name__icontains=query)
        )

    if category_id:
        books = books.filter(category_id=category_id)

    if status_filter:
        books = books.filter(status=status_filter)

    # Sorting
    if sort_by == 'author':
        books = books.order_by('author')
    elif sort_by == 'newest':
        books = books.order_by('-created_at')
    elif sort_by == 'available':
        books = books.order_by('-available_copies')
    else:
        books = books.order_by('title')

    categories = Category.objects.all()

    context = {
        'books': books,
        'categories': categories,
        'query': query,
        'selected_category': category_id,
        'selected_status': status_filter,
        'selected_sort': sort_by,
    }
    return render(request, 'library_app/book_list.html', context)


@login_required
@librarian_required
def book_create_view(request):
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('isbn', '').strip()
        category_id = request.POST.get('category')
        shelf_number = request.POST.get('shelf_number', '').strip()
        publication_year = request.POST.get('publication_year') or None
        copies = int(request.POST.get('copies', 1))
        cover_image_url = request.POST.get('cover_image_url', '').strip()
        description = request.POST.get('description', '').strip()

        if Book.objects.filter(isbn=isbn).exists():
            messages.error(request, f"A book with ISBN '{isbn}' already exists.")
            return redirect('book_list')

        category = get_object_or_404(Category, id=category_id)
        book = Book.objects.create(
            title=title,
            author=author,
            isbn=isbn,
            category=category,
            shelf_number=shelf_number,
            publication_year=publication_year,
            total_copies=copies,
            available_copies=copies,
            status='AVAILABLE',
            cover_image_url=cover_image_url,
            description=description
        )

        log_audit(request, 'CREATE_BOOK', 'Book', book.id, f"Added book '{title}' ({isbn})")
        messages.success(request, f"Book '{book.title}' added to the catalog.")
        return redirect('book_list')

    return redirect('book_list')


@login_required
@librarian_required
def book_edit_view(request, book_id):
    book = get_object_or_404(Book, id=book_id)

    if request.method == 'POST':
        book.title = request.POST.get('title', '').strip()
        book.author = request.POST.get('author', '').strip()
        book.isbn = request.POST.get('isbn', '').strip()
        category_id = request.POST.get('category')
        if category_id:
            book.category = get_object_or_404(Category, id=category_id)
        book.shelf_number = request.POST.get('shelf_number', '').strip()
        pub_year = request.POST.get('publication_year')
        book.publication_year = int(pub_year) if pub_year else None
        book.status = request.POST.get('status', book.status)
        book.cover_image_url = request.POST.get('cover_image_url', book.cover_image_url).strip()
        book.description = request.POST.get('description', '').strip()
        book.save()

        log_audit(request, 'UPDATE_BOOK', 'Book', book.id, f"Updated book '{book.title}'")
        messages.success(request, f"Book '{book.title}' updated successfully.")
        return redirect('book_detail', book_id=book.id)

    categories = Category.objects.all()
    return render(request, 'library_app/book_edit.html', {'book': book, 'categories': categories})


@login_required
@librarian_required
def book_delete_view(request, book_id):
    book = get_object_or_404(Book, id=book_id)

    # Prevent deleting if active borrow transactions exist
    if book.borrows.filter(status='BORROWED').exists():
        messages.error(request, f"Cannot delete '{book.title}' because it has active borrow transactions.")
        return redirect('book_list')

    title = book.title
    book.delete()
    log_audit(request, 'DELETE_BOOK', 'Book', book_id, f"Deleted book '{title}'")
    messages.success(request, f"Book '{title}' was deleted from the catalog.")
    return redirect('book_list')


@login_required
def book_detail_view(request, book_id):
    book = get_object_or_404(Book.objects.select_related('category'), id=book_id)
    active_borrows = book.borrows.filter(status='BORROWED').select_related('member')
    reservations = book.reservations.select_related('member').all()
    categories = Category.objects.all()

    # Check if current member has already reserved or borrowed this book
    user_has_reserved = False
    user_has_borrowed = False
    if hasattr(request.user, 'member_account'):
        member = request.user.member_account
        user_has_reserved = book.reservations.filter(member=member, status__in=['PENDING', 'READY']).exists()
        user_has_borrowed = book.borrows.filter(member=member, status='BORROWED').exists()

    context = {
        'book': book,
        'categories': categories,
        'active_borrows': active_borrows,
        'reservations': reservations,
        'user_has_reserved': user_has_reserved,
        'user_has_borrowed': user_has_borrowed,
    }
    return render(request, 'library_app/book_detail.html', context)


# ==========================================
# MODULE 3: BORROWING & RETURNING TRANSACTIONS
# ==========================================

@login_required
@librarian_required
def transaction_list_view(request):
    status_filter = request.GET.get('status', '').strip()
    query = request.GET.get('q', '').strip()

    transactions = BorrowTransaction.objects.select_related('book', 'member', 'issued_by').all()

    if status_filter:
        transactions = transactions.filter(status=status_filter)
    if query:
        transactions = transactions.filter(
            Q(transaction_code__icontains=query) |
            Q(book__title__icontains=query) |
            Q(member__first_name__icontains=query) |
            Q(member__last_name__icontains=query) |
            Q(member__member_id__icontains=query)
        )

    # Sync overdue fines for active transactions
    for txn in transactions:
        if txn.is_currently_overdue:
            txn.fine_amount = txn.compute_fine()

    members = Member.objects.filter(status='ACTIVE')
    available_books = Book.objects.filter(available_copies__gt=0, status__in=['AVAILABLE', 'RESERVED'])

    context = {
        'transactions': transactions,
        'status_filter': status_filter,
        'query': query,
        'members': members,
        'available_books': available_books,
    }
    return render(request, 'library_app/transaction_list.html', context)


@login_required
@librarian_required
def borrow_create_view(request):
    if request.method == 'POST':
        member_id = request.POST.get('member_id')
        book_id = request.POST.get('book_id')
        days = int(request.POST.get('borrow_days', 14))

        member = get_object_or_404(Member, id=member_id)
        book = get_object_or_404(Book, id=book_id)

        # Validations
        if member.status != 'ACTIVE':
            messages.error(request, f"Member {member.member_id} is inactive or suspended.")
            return redirect('transaction_list')

        if book.available_copies <= 0:
            messages.error(request, f"Book '{book.title}' currently has no copies available for checkout.")
            return redirect('transaction_list')

        # Create transaction
        issue_date = timezone.now().date()
        due_date = issue_date + timedelta(days=days)
        txn_code = BorrowTransaction.generate_transaction_code()

        txn = BorrowTransaction.objects.create(
            transaction_code=txn_code,
            member=member,
            book=book,
            issued_by=request.user,
            issue_date=issue_date,
            due_date=due_date,
            status='BORROWED'
        )

        # Update book availability
        book.available_copies -= 1
        book.update_availability_status()

        # Fulfill reservation if member had reserved it
        pending_res = Reservation.objects.filter(member=member, book=book, status__in=['PENDING', 'READY']).first()
        if pending_res:
            pending_res.status = 'FULFILLED'
            pending_res.save()

        log_audit(request, 'CHECKOUT_BOOK', 'BorrowTransaction', txn.id, f"Issued '{book.title}' to {member.full_name} ({txn_code})")
        messages.success(request, f"Book '{book.title}' successfully checked out to {member.full_name}. Due on {due_date.strftime('%b %d, %Y')}.")
        return redirect('transaction_list')

    return redirect('transaction_list')


@login_required
@librarian_required
def return_process_view(request, transaction_id):
    txn = get_object_or_404(BorrowTransaction, id=transaction_id)

    if txn.status == 'RETURNED':
        messages.info(request, "This transaction has already been completed.")
        return redirect('transaction_list')

    today = timezone.now().date()
    txn.return_date = today
    txn.returned_to = request.user
    txn.status = 'RETURNED'

    # Compute overdue fine if returned late
    fine = txn.compute_fine()
    txn.fine_amount = fine
    if fine > Decimal('0.00'):
        txn.fine_paid = False
        txn.member.balance += fine
        txn.member.save()
        messages.warning(request, f"Book returned late by {txn.days_overdue} day(s). Late fee assessed: ₱{fine:.2f}.")
    else:
        messages.success(request, f"Book '{txn.book.title}' returned on time. No fines.")

    txn.save()

    # Update book copy count and check reservations
    book = txn.book
    book.available_copies += 1
    next_reservation = notify_reservation_ready(book)
    if next_reservation:
        messages.info(request, f"Book '{book.title}' has a pending reservation for {next_reservation.member.full_name}. Notified member for pickup.")

    log_audit(request, 'RETURN_BOOK', 'BorrowTransaction', txn.id, f"Processed return for {txn.transaction_code}. Fine: ₱{fine:.2f}")
    return redirect('transaction_list')


# ==========================================
# MODULE 4: RESERVATION MANAGEMENT
# ==========================================

@login_required
def reservation_list_view(request):
    user = request.user
    role = getattr(user.profile, 'role', 'MEMBER') if hasattr(user, 'profile') else 'ADMIN'

    if role == 'MEMBER':
        member = get_object_or_404(Member, user=user)
        reservations = Reservation.objects.filter(member=member).select_related('book')
    else:
        reservations = Reservation.objects.select_related('book', 'member').all()

    context = {
        'reservations': reservations,
        'role': role,
    }
    return render(request, 'library_app/reservation_list.html', context)


@login_required
def reserve_book_view(request, book_id):
    book = get_object_or_404(Book, id=book_id)

    # Determine which member is reserving
    if hasattr(request.user, 'member_account'):
        member = request.user.member_account
    else:
        # If librarian is reserving on behalf of a member
        member_id = request.POST.get('member_id')
        if not member_id:
            messages.error(request, "Please select a member to place the reservation.")
            return redirect('book_detail', book_id=book.id)
        member = get_object_or_404(Member, id=member_id)

    # Check if member already has pending reservation for this book
    if Reservation.objects.filter(book=book, member=member, status__in=['PENDING', 'READY']).exists():
        messages.warning(request, f"{member.full_name} already has an active reservation for '{book.title}'.")
        return redirect('book_detail', book_id=book.id)

    # Create reservation
    res = Reservation.objects.create(
        book=book,
        member=member,
        status='PENDING',
        notes="Reserved online by member" if hasattr(request.user, 'member_account') else f"Reserved by staff {request.user.username}"
    )

    log_audit(request, 'RESERVE_BOOK', 'Reservation', res.id, f"Reserved '{book.title}' for {member.full_name}")
    messages.success(request, f"Reservation confirmed for '{book.title}'. You will be alerted when it is ready for collection.")
    return redirect('book_detail', book_id=book.id)


@login_required
def cancel_reservation_view(request, reservation_id):
    res = get_object_or_404(Reservation, id=reservation_id)

    # Only owner or librarian can cancel
    if hasattr(request.user, 'profile') and request.user.profile.is_member:
        if res.member.user != request.user:
            messages.error(request, "Unauthorized action.")
            return redirect('reservation_list')

    res.status = 'CANCELLED'
    res.save()
    res.book.update_availability_status()

    log_audit(request, 'CANCEL_RESERVATION', 'Reservation', res.id, f"Cancelled reservation for '{res.book.title}'")
    messages.info(request, f"Reservation {res.reservation_code} cancelled.")
    return redirect('reservation_list')


# ==========================================
# MODULE 5: FINE & PAYMENT PROCESSING
# ==========================================

@login_required
@librarian_required
def fines_list_view(request):
    overdue_txns = BorrowTransaction.objects.filter(
        fine_amount__gt=0
    ).select_related('book', 'member')

    recent_payments = FinePayment.objects.select_related('transaction', 'member', 'collected_by')[:20]

    context = {
        'overdue_txns': overdue_txns,
        'recent_payments': recent_payments,
    }
    return render(request, 'library_app/fines_list.html', context)


@login_required
@librarian_required
def process_fine_payment_view(request, transaction_id):
    txn = get_object_or_404(BorrowTransaction, id=transaction_id)

    if request.method == 'POST':
        amount = Decimal(request.POST.get('amount', '0.00'))
        method = request.POST.get('payment_method', 'CASH')
        notes = request.POST.get('notes', '')

        if amount <= Decimal('0.00'):
            messages.error(request, "Payment amount must be greater than zero.")
            return redirect('fines_list')

        # Record payment
        receipt_num = FinePayment.generate_receipt_number()
        payment = FinePayment.objects.create(
            receipt_number=receipt_num,
            transaction=txn,
            member=txn.member,
            amount_paid=amount,
            payment_method=method,
            collected_by=request.user,
            notes=notes
        )

        # Update member balance
        txn.member.balance = max(Decimal('0.00'), txn.member.balance - amount)
        txn.member.save()

        # Update transaction fine status
        if amount >= txn.fine_amount:
            txn.fine_paid = True
            txn.save()

        # Member notification
        if txn.member.user:
            Notification.objects.create(
                recipient=txn.member.user,
                notification_type='PAYMENT',
                title=f"Payment Receipt: {receipt_num}",
                message=f"Received payment of ₱{amount:.2f} for '{txn.book.title}'. Receipt #{receipt_num}."
            )

        log_audit(request, 'COLLECT_FINE', 'FinePayment', payment.id, f"Collected ₱{amount:.2f} fine from {txn.member.full_name} ({receipt_num})")
        messages.success(request, f"Payment of ₱{amount:.2f} recorded! Receipt: {receipt_num}")
        return redirect('receipt_view', receipt_number=receipt_num)

    return redirect('fines_list')


@login_required
def receipt_view(request, receipt_number):
    payment = get_object_or_404(FinePayment.objects.select_related('transaction', 'member', 'collected_by'), receipt_number=receipt_number)

    # Permission check for members
    if hasattr(request.user, 'profile') and request.user.profile.is_member:
        if payment.member.user != request.user:
            messages.error(request, "Unauthorized receipt view.")
            return redirect('dashboard')

    return render(request, 'library_app/receipt.html', {'payment': payment})


@login_required
def receipt_download_pdf_view(request, receipt_number):
    payment = get_object_or_404(FinePayment.objects.select_related('transaction', 'member', 'collected_by'), receipt_number=receipt_number)
    pdf_bytes = generate_receipt_pdf(payment)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Receipt_{receipt_number}.pdf"'
    return response


# ==========================================
# MODULE 6: REPORTS & STATISTICS
# ==========================================

@login_required
@librarian_required
def reports_dashboard_view(request):
    today = timezone.now().date()

    # 1. Borrowed and Overdue stats
    borrowed_count = BorrowTransaction.objects.filter(status='BORROWED').count()
    overdue_count = BorrowTransaction.objects.filter(status='BORROWED', due_date__lt=today).count()

    # 2. Most borrowed books
    most_borrowed = Book.objects.annotate(borrow_count=Count('borrows')).order_by('-borrow_count')[:10]

    # 3. Active vs Inactive members
    active_members_count = Member.objects.filter(status='ACTIVE').count()
    inactive_members_count = Member.objects.filter(status='INACTIVE').count()
    suspended_members_count = Member.objects.filter(status='SUSPENDED').count()

    # 4. Monthly transactions (last 6 months)
    transactions_monthly = (
        BorrowTransaction.objects.values('issue_date__month')
        .annotate(total=Count('id'))
        .order_by('issue_date__month')
    )

    # Categories breakdown
    category_counts = Category.objects.annotate(book_count=Count('books'))

    context = {
        'borrowed_count': borrowed_count,
        'overdue_count': overdue_count,
        'most_borrowed': most_borrowed,
        'active_members_count': active_members_count,
        'inactive_members_count': inactive_members_count,
        'suspended_members_count': suspended_members_count,
        'transactions_monthly': transactions_monthly,
        'category_counts': category_counts,
    }
    return render(request, 'library_app/reports_dashboard.html', context)


@login_required
@librarian_required
def export_report_view(request):
    report_type = request.GET.get('type', 'borrowed_overdue')
    export_format = request.GET.get('format', 'excel')

    today = timezone.now().date()
    query_data = None
    title = ""

    if report_type == 'borrowed_overdue':
        title = "Borrowed and Overdue Books Report"
        query_data = BorrowTransaction.objects.filter(
            Q(status='BORROWED') | Q(status='OVERDUE')
        ).select_related('book', 'member')
    elif report_type == 'most_borrowed':
        title = "Most Borrowed Books Summary"
        query_data = (
            Book.objects.annotate(borrow_count=Count('borrows'))
            .values('title', 'author', 'isbn', 'category__name', 'borrow_count')
            .order_by('-borrow_count')
        )
    elif report_type == 'members_status':
        title = "Members Status and Balances Report"
        query_data = Member.objects.all().order_by('status', 'last_name')
    elif report_type == 'transactions_summary':
        title = "Transactions Summary Report"
        query_data = BorrowTransaction.objects.select_related('book', 'member', 'issued_by').all()[:100]

    if export_format == 'excel':
        excel_bytes = export_report_excel(report_type, query_data, title)
        response = HttpResponse(
            excel_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{report_type}_{today.strftime("%Y%m%d")}.xlsx"'
        return response
    elif export_format == 'pdf':
        pdf_bytes = export_report_pdf(report_type, query_data, title)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{report_type}_{today.strftime("%Y%m%d")}.pdf"'
        return response

    return redirect('reports_dashboard')


# ==========================================
# MODULE 7 & 10: USER MANAGEMENT, BACKUP & SECURITY
# ==========================================

@login_required
@admin_required
def user_management_view(request):
    users = User.objects.select_related('profile').all().order_by('-date_joined')
    audit_logs = AuditLog.objects.select_related('user').all()[:25]

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_staff':
            username = request.POST.get('username', '').strip()
            password = request.POST.get('password', '')
            role = request.POST.get('role', 'LIBRARIAN')
            email = request.POST.get('email', '').strip()
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()

            if User.objects.filter(username=username).exists():
                messages.error(request, f"Username '{username}' already exists.")
            else:
                user = User.objects.create_user(
                    username=username, email=email, password=password,
                    first_name=first_name, last_name=last_name
                )
                UserProfile.objects.create(user=user, role=role)
                log_audit(request, 'CREATE_STAFF_USER', 'User', user.id, f"Created staff user {username} ({role})")
                messages.success(request, f"User '{username}' created with role: {role}")
                return redirect('user_management')

    context = {
        'users': users,
        'audit_logs': audit_logs,
    }
    return render(request, 'library_app/user_management.html', context)


@login_required
@admin_required
def backup_download_view(request):
    backup_data = create_database_backup()
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    response = HttpResponse(backup_data, content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="library_backup_{timestamp}.json"'
    log_audit(request, 'DATABASE_BACKUP_EXPORT', 'System', 0, "Admin initiated full database backup export.")
    return response


@login_required
@admin_required
def backup_restore_view(request):
    if request.method == 'POST' and request.FILES.get('backup_file'):
        file = request.FILES['backup_file']
        try:
            content = file.read().decode('utf-8')
            count = restore_database_backup(content)
            log_audit(request, 'DATABASE_BACKUP_RESTORE', 'System', 0, f"Restored {count} records from backup.")
            messages.success(request, f"Successfully restored {count} records from backup file.")
        except Exception as e:
            messages.error(request, f"Error restoring backup: {str(e)}")
    return redirect('user_management')


# ==========================================
# MODULE 9: NOTIFICATIONS
# ==========================================

@login_required
def notification_list_view(request):
    notifications = Notification.objects.filter(recipient=request.user)
    unread_count = notifications.filter(is_read=False).count()
    return render(request, 'library_app/notifications.html', {
        'notifications': notifications,
        'unread_count': unread_count
    })


@login_required
def mark_notification_read_view(request, notification_id):
    notif = get_object_or_404(Notification, id=notification_id, recipient=request.user)
    notif.is_read = True
    notif.save()
    return JsonResponse({'status': 'ok'})


@login_required
def mark_all_notifications_read_view(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    messages.info(request, "All notifications marked as read.")
    return redirect('notification_list')
