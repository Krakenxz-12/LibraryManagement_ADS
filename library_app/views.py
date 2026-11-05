from datetime import timedelta, date
from decimal import Decimal, InvalidOperation
import json
import secrets

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Q, Count, Sum
from django.utils import timezone

from .models import (
    UserProfile, Category, Member, Book, BorrowTransaction,
    Reservation, FinePayment, Notification, AuditLog, BorrowRequest, LibrarySettings, ACTIVE_LOAN_STATUSES
)
from .forms import LibrarySettingsForm
from .services import (
    HoldError, weekly_allowance, expire_stale_holds, promote_reservations, submit_borrow_request,
    approve_request, reject_request, cancel_request, confirm_pickup, place_reservation,
    cancel_reservation as cancel_reservation_service, confirm_reservation_pickup, checkout_at_desk,
)
from .reports import (
    REPORT_TITLES, REPORT_TYPES, build_report, category_rows, export_excel, export_pdf,
    overview_stats, parse_range, top_books, top_borrowers,
)
from .utils import (
    role_required, librarian_required, admin_required,
    log_audit, check_and_create_alerts,
    generate_receipt_pdf,
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

    today = timezone.localdate()
    user = request.user
    role = getattr(user.profile, 'role', 'MEMBER') if hasattr(user, 'profile') else 'ADMIN'

    # Key statistics
    total_books = Book.objects.count()
    available_books = Book.objects.filter(status='AVAILABLE').count()
    borrowed_books = Book.objects.filter(status='BORROWED').count()
    overdue_books = BorrowTransaction.objects.filter(status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today).count()
    total_members = Member.objects.count()
    active_members = Member.objects.filter(status='ACTIVE').count()
    unpaid_fines = BorrowTransaction.objects.filter(fine_amount__gt=0, fine_paid=False).aggregate(total=Sum('fine_amount'))['total'] or Decimal('0.00')

    # Recent transactions
    recent_transactions = BorrowTransaction.objects.select_related('book', 'member').all()[:6]

    # Overdue list
    overdue_txns = BorrowTransaction.objects.filter(
        status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today
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
            member_active_borrows = BorrowTransaction.objects.filter(member=member_profile, status__in=ACTIVE_LOAN_STATUSES)
            member_reservations = Reservation.objects.filter(member=member_profile).exclude(status__in=['FULFILLED', 'CANCELLED'])

    # Recent list of books for visual carousel/grid
    recent_books = Book.objects.select_related('category').all().order_by('-created_at')[:10]

    # Borrow-request / pickup counters
    pending_requests_count = awaiting_pickup_count = 0
    allowance = None
    member_pending_requests = member_awaiting_pickup = 0
    if role == 'MEMBER':
        if member_profile:
            allowance = weekly_allowance(member_profile)
            member_pending_requests = member_profile.borrow_requests.filter(status='PENDING').count()
            member_awaiting_pickup = (
                member_profile.borrow_requests.filter(status='APPROVED').count()
                + member_profile.reservations.filter(status='READY').count()
            )
    else:
        pending_requests_count = BorrowRequest.objects.filter(status='PENDING').count()
        awaiting_pickup_count = (
            BorrowRequest.objects.filter(status='APPROVED').count()
            + Reservation.objects.filter(status='READY').count()
        )

    is_admin_user = role == 'ADMIN' or user.is_superuser
    active_librarians = (
        User.objects.filter(profile__role='LIBRARIAN', is_active=True).count() if is_admin_user else 0
    )

    context = {
        'is_admin_user': is_admin_user,
        'active_librarians': active_librarians,
        'pending_requests_count': pending_requests_count,
        'awaiting_pickup_count': awaiting_pickup_count,
        'allowance': allowance,
        'member_pending_requests': member_pending_requests,
        'member_awaiting_pickup': member_awaiting_pickup,
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
        membership_type = request.POST.get('membership_type', 'STUDENT')
        if membership_type not in dict(Member.MEMBERSHIP_TYPE_CHOICES):
            membership_type = 'STUDENT'

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
            membership_type=membership_type,
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
        new_type = request.POST.get('membership_type', member.membership_type)
        if new_type in dict(Member.MEMBERSHIP_TYPE_CHOICES):
            member.membership_type = new_type
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

    # Books with any borrow history are protected (PROTECT FK) - block with a message instead of a 500 error
    if book.borrows.exists():
        messages.error(request, f"Cannot delete '{book.title}' because it has borrowing history. Set its status to Lost or Under Repair instead.")
        return redirect('book_list')

    title = book.title
    book.delete()
    log_audit(request, 'DELETE_BOOK', 'Book', book_id, f"Deleted book '{title}'")
    messages.success(request, f"Book '{title}' was deleted from the catalog.")
    return redirect('book_list')


@login_required
def book_detail_view(request, book_id):
    book = get_object_or_404(Book.objects.select_related('category'), id=book_id)
    active_borrows = book.borrows.filter(status__in=ACTIVE_LOAN_STATUSES).select_related('member')
    reservations = book.reservations.select_related('member').all()
    categories = Category.objects.all()

    # Check if current member has already reserved or borrowed this book
    user_has_reserved = False
    user_has_borrowed = False
    user_request = None
    if hasattr(request.user, 'member_account'):
        member = request.user.member_account
        user_has_reserved = book.reservations.filter(member=member, status__in=['PENDING', 'READY']).exists()
        user_has_borrowed = book.borrows.filter(member=member, status__in=ACTIVE_LOAN_STATUSES).exists()
        user_request = book.borrow_requests.filter(member=member, status__in=['PENDING', 'APPROVED']).first()

    context = {
        'book': book,
        'categories': categories,
        'active_borrows': active_borrows,
        'reservations': reservations,
        'user_has_reserved': user_has_reserved,
        'user_has_borrowed': user_has_borrowed,
        'user_request': user_request,
        'waiting_count': (
            book.reservations.filter(status__in=['PENDING', 'READY']).count()
            + book.borrow_requests.filter(status__in=['PENDING', 'APPROVED']).count()
        ),
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
    """Walk-in checkout at the desk (also used to hand over a member's held book)."""
    if request.method == 'POST':
        member = get_object_or_404(Member, id=request.POST.get('member_id'))
        book = get_object_or_404(Book, id=request.POST.get('book_id'))
        try:
            txn = checkout_at_desk(member, book.id, request.user)  # duration comes from Library Settings
        except HoldError as e:
            messages.error(request, str(e))
            return redirect('transaction_list')

        log_audit(request, 'CHECKOUT_BOOK', 'BorrowTransaction', txn.id, f"Issued '{book.title}' to {member.full_name} ({txn.transaction_code})")
        messages.success(request, f"Book '{book.title}' successfully checked out to {member.full_name}. Due on {txn.due_date.strftime('%b %d, %Y')}.")
        return redirect('transaction_list')

    return redirect('transaction_list')


@login_required
@librarian_required
def return_process_view(request, transaction_id):
    with transaction.atomic():
        txn = get_object_or_404(
            BorrowTransaction.objects.select_for_update().select_related('member', 'book'),
            id=transaction_id
        )

        if txn.status == 'RETURNED':
            messages.info(request, "This transaction has already been completed.")
            return redirect('transaction_list')

        today = timezone.localdate()
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

        # Put the copy back on the shelf (save the count!) and check reservations
        book = Book.objects.select_for_update().get(pk=txn.book_id)
        book.available_copies = min(book.available_copies + 1, book.total_copies)
        book.save(update_fields=['available_copies'])
        promoted = promote_reservations(book)
        if promoted:
            nxt = promoted[0]
            messages.info(request, f"'{book.title}' is now held for {nxt.member.full_name} (next in the reservation queue). They have until {timezone.localtime(nxt.pickup_deadline).strftime('%b %d, %I:%M %p')} to collect it.")

    log_audit(request, 'RETURN_BOOK', 'BorrowTransaction', txn.id, f"Processed return for {txn.transaction_code}. Fine: ₱{fine:.2f}")
    return redirect('transaction_list')


# ==========================================
# MODULE 4: RESERVATION MANAGEMENT
# ==========================================

@login_required
def reservation_list_view(request):
    expire_stale_holds()
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
@require_POST
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

    try:
        res = place_reservation(member, book)
    except HoldError as e:
        messages.warning(request, str(e))
        return redirect('book_detail', book_id=book.id)

    log_audit(request, 'RESERVE_BOOK', 'Reservation', res.id, f"Reserved '{book.title}' for {member.full_name}")
    messages.success(request, f"Reservation confirmed for '{book.title}'. You are in the queue and will be alerted when it is ready for pickup.")
    return redirect('book_detail', book_id=book.id)


@login_required
@require_POST
def cancel_reservation_view(request, reservation_id):
    res = get_object_or_404(Reservation, id=reservation_id)

    # Only owner or librarian can cancel
    if hasattr(request.user, 'profile') and request.user.profile.is_member:
        if res.member.user != request.user:
            messages.error(request, "Unauthorized action.")
            return redirect('reservation_list')

    try:
        cancel_reservation_service(res.id)
    except HoldError as e:
        messages.warning(request, str(e))
        return redirect('reservation_list')

    log_audit(request, 'CANCEL_RESERVATION', 'Reservation', res.id, f"Cancelled reservation for '{res.book.title}'")
    messages.info(request, f"Reservation {res.reservation_code} cancelled.")
    return redirect('reservation_list')


@login_required
@librarian_required
@require_POST
def reservation_pickup_view(request, reservation_id):
    """Librarian confirms the member physically collected a reserved (held) book."""
    expire_stale_holds()
    res = get_object_or_404(Reservation, id=reservation_id)
    try:
        txn = confirm_reservation_pickup(res.id, request.user)
    except HoldError as e:
        messages.error(request, str(e))
        return redirect(request.POST.get('next') or 'reservation_list')

    log_audit(request, 'RESERVATION_PICKUP', 'BorrowTransaction', txn.id, f"Handed over reserved '{res.book.title}' to {res.member.full_name}")
    messages.success(request, f"'{res.book.title}' handed to {res.member.full_name}. Due on {txn.due_date.strftime('%b %d, %Y')}.")
    return redirect(request.POST.get('next') or 'reservation_list')


# ==========================================
# BORROW REQUESTS (request -> approve -> pickup -> borrowed)
# ==========================================

@login_required
@require_POST
def request_borrow_view(request, book_id):
    book = get_object_or_404(Book, id=book_id)
    member = Member.objects.filter(user=request.user).first()
    if not member:
        messages.error(request, "Your account is not linked to a library membership. Please see the librarian.")
        return redirect('book_detail', book_id=book.id)

    try:
        req = submit_borrow_request(member, book)
    except HoldError as e:
        messages.warning(request, str(e))
        return redirect('book_detail', book_id=book.id)

    log_audit(request, 'REQUEST_BORROW', 'BorrowRequest', req.id, f"{member.full_name} requested '{book.title}'")
    messages.success(request, f"Request sent for '{book.title}'. The librarian will review it, and you'll be notified when it's ready for pickup.")
    return redirect('my_requests')


@login_required
@librarian_required
def borrow_request_list_view(request):
    expire_stale_holds()
    pending = list(BorrowRequest.objects.filter(status='PENDING').select_related('member', 'book').order_by('requested_at'))
    for r in pending:
        r.allowance = weekly_allowance(r.member)
    awaiting = list(BorrowRequest.objects.filter(status='APPROVED').select_related('member', 'book').order_by('pickup_deadline'))
    ready_reservations = list(Reservation.objects.filter(status='READY').select_related('member', 'book').order_by('pickup_deadline'))
    context = {
        'pending': pending,
        'awaiting': awaiting,
        'ready_reservations': ready_reservations,
        'on_hold_total': len(awaiting) + len(ready_reservations),
        'history': BorrowRequest.objects.exclude(status__in=['PENDING', 'APPROVED']).select_related('member', 'book', 'handled_by')[:30],
        'hold_hours': settings.PICKUP_HOLD_HOURS,
    }
    return render(request, 'library_app/borrow_request_list.html', context)


@login_required
@librarian_required
@require_POST
def request_approve_view(request, request_id):
    expire_stale_holds()
    req = get_object_or_404(BorrowRequest, id=request_id)
    try:
        approve_request(req.id, request.user)
    except HoldError as e:
        messages.error(request, str(e))
        return redirect('borrow_request_list')
    log_audit(request, 'APPROVE_REQUEST', 'BorrowRequest', req.id, f"Approved '{req.book.title}' for {req.member.full_name}")
    messages.success(request, f"Approved. '{req.book.title}' is held for {req.member.full_name} for {settings.PICKUP_HOLD_HOURS} hours.")
    return redirect('borrow_request_list')


@login_required
@librarian_required
@require_POST
def request_reject_view(request, request_id):
    req = get_object_or_404(BorrowRequest, id=request_id)
    try:
        reject_request(req.id, request.user, request.POST.get('reason', ''))
    except HoldError as e:
        messages.error(request, str(e))
        return redirect('borrow_request_list')
    log_audit(request, 'REJECT_REQUEST', 'BorrowRequest', req.id, f"Rejected '{req.book.title}' for {req.member.full_name}")
    messages.info(request, f"Request {req.request_code} rejected.")
    return redirect('borrow_request_list')


@login_required
@librarian_required
@require_POST
def request_pickup_view(request, request_id):
    """Librarian confirms the member physically received the book. The loan period starts now."""
    expire_stale_holds()
    req = get_object_or_404(BorrowRequest, id=request_id)
    try:
        txn = confirm_pickup(req.id, request.user)
    except HoldError as e:
        messages.error(request, str(e))
        return redirect('borrow_request_list')
    log_audit(request, 'CONFIRM_PICKUP', 'BorrowTransaction', txn.id, f"Handed over '{req.book.title}' to {req.member.full_name} ({txn.transaction_code})")
    messages.success(request, f"'{req.book.title}' handed to {req.member.full_name}. Due on {txn.due_date.strftime('%b %d, %Y')}.")
    return redirect('borrow_request_list')


@login_required
@require_POST
def request_cancel_view(request, request_id):
    req = get_object_or_404(BorrowRequest, id=request_id)
    is_member = hasattr(request.user, 'profile') and request.user.profile.is_member
    if is_member and req.member.user != request.user:
        messages.error(request, "Unauthorized action.")
        return redirect('my_requests')
    try:
        cancel_request(req.id, request.user)
    except HoldError as e:
        messages.warning(request, str(e))
    else:
        log_audit(request, 'CANCEL_REQUEST', 'BorrowRequest', req.id, f"Cancelled request for '{req.book.title}'")
        messages.info(request, f"Request {req.request_code} cancelled.")
    return redirect('my_requests' if is_member else 'borrow_request_list')


@login_required
def my_requests_view(request):
    expire_stale_holds()
    member = Member.objects.filter(user=request.user).first()
    if not member:
        return redirect('dashboard')
    context = {
        'member': member,
        'allowance': weekly_allowance(member),
        'requests': member.borrow_requests.select_related('book'),
        'ready_reservations': member.reservations.filter(status='READY').select_related('book'),
        'active_reservation_total': member.reservations.filter(status__in=['PENDING', 'READY']).count(),
        'borrowed_count': member.borrows.filter(status__in=ACTIVE_LOAN_STATUSES).count(),
    }
    return render(request, 'library_app/my_requests.html', context)


# ==========================================
# MODULE 5: FINE & PAYMENT PROCESSING
# ==========================================

@login_required
def my_fines_view(request):
    """Member-facing page: own fines (with balance) and payment receipts."""
    profile = getattr(request.user, 'profile', None)
    if not (profile and profile.is_member) or request.user.is_superuser:
        return redirect('fines_list')  # staff use the full Fines & Payments page

    member = Member.objects.filter(user=request.user).first()
    fine_rows, payments = [], []
    total_outstanding = total_paid = Decimal('0.00')

    if member:
        txns = (
            BorrowTransaction.objects.filter(member=member, fine_amount__gt=0)
            .select_related('book').prefetch_related('payments')
            .order_by('-issue_date', '-id')
        )
        for txn in txns:
            paid = sum((p.amount_paid for p in txn.payments.all()), Decimal('0.00'))
            outstanding = max(txn.fine_amount - paid, Decimal('0.00'))
            fine_rows.append({'txn': txn, 'paid': paid, 'outstanding': outstanding})
            total_outstanding += outstanding
        payments = member.payments.select_related('transaction__book')
        total_paid = sum((p.amount_paid for p in payments), Decimal('0.00'))

    return render(request, 'library_app/my_fines.html', {
        'member': member,
        'fine_rows': fine_rows,
        'unpaid_fines': [r for r in fine_rows if r['outstanding'] > 0],
        'payments': payments,
        'total_outstanding': total_outstanding,
        'total_paid': total_paid,
    })


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
        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
        except InvalidOperation:
            messages.error(request, "Please enter a valid payment amount.")
            return redirect('fines_list')
        method = request.POST.get('payment_method', 'CASH')
        notes = request.POST.get('notes', '')

        if amount <= Decimal('0.00'):
            messages.error(request, "Payment amount must be greater than zero.")
            return redirect('fines_list')

        already_paid = txn.payments.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
        outstanding = txn.fine_amount - already_paid
        if amount > outstanding:
            messages.error(request, f"Payment of ₱{amount:.2f} is more than the outstanding fine of ₱{max(outstanding, Decimal('0.00')):.2f}.")
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
        if already_paid + amount >= txn.fine_amount:
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
@admin_required
def reports_dashboard_view(request):
    start, end, note = parse_range(request.GET)
    if note:
        messages.info(request, note)
    context = {
        'start': start, 'end': end,
        'stats': overview_stats(start, end),
        'top_books': top_books(start, end, 10),
        'top_borrowers': top_borrowers(start, end, 10),
        'category_rows': category_rows(start, end),
        'report_types': REPORT_TYPES,
        'query': f"start={start.isoformat()}&end={end.isoformat()}",
    }
    return render(request, 'library_app/reports_dashboard.html', context)


@login_required
@admin_required
def export_report_view(request):
    report_type = request.GET.get('type', 'overview')
    export_format = request.GET.get('format', 'excel')
    start, end, _ = parse_range(request.GET)
    if report_type not in REPORT_TITLES:
        messages.error(request, "Unknown report type.")
        return redirect('reports_dashboard')

    report = build_report(report_type, start, end)
    stamp = timezone.localdate().strftime("%Y%m%d")
    log_audit(request, 'EXPORT_REPORT', 'Report', report_type, f"Exported '{report['title']}' as {export_format} ({start} to {end})")

    if export_format == 'pdf':
        response = HttpResponse(export_pdf(report), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{report_type}_{stamp}.pdf"'
        return response
    response = HttpResponse(
        export_excel(report),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{report_type}_{stamp}.xlsx"'
    return response


# ==========================================
# ADMIN: SYSTEM SETTINGS & CATEGORIES
# ==========================================

SETTING_LABELS = {
    'daily_fine_rate': 'Fine per overdue day',
    'weekly_borrow_limit': 'Weekly borrowing limit',
    'borrow_period_days': 'Borrowing duration (days)',
}


@login_required
@admin_required
def system_settings_view(request):
    rules = LibrarySettings.get()
    if request.method == 'POST':
        form = LibrarySettingsForm(request.POST, instance=rules)
        if form.is_valid():
            changes = [
                f"{SETTING_LABELS[f]}: {form.initial[f]} -> {form.cleaned_data[f]}" for f in form.changed_data
            ]
            if changes:
                obj = form.save(commit=False)
                obj.updated_by = request.user
                obj.save()
                log_audit(request, 'UPDATE_SETTINGS', 'LibrarySettings', 1, "; ".join(changes))
                messages.success(request, "Settings saved. They apply to new loans and calculations; existing loans keep their original due date and fine rate.")
            else:
                messages.info(request, "No changes to save.")
            return redirect('system_settings')
        messages.error(request, "Please fix the highlighted settings.")
    else:
        form = LibrarySettingsForm(instance=rules)

    return render(request, 'library_app/system_settings.html', {
        'form': form,
        'current': LibrarySettings.get(),
        'categories': Category.objects.annotate(book_count=Count('books')).order_by('name'),
    })


def _category_name_taken(name, exclude_id=None):
    qs = Category.objects.filter(name__iexact=name)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.exists()


@login_required
@admin_required
@require_POST
def category_create_view(request):
    name = request.POST.get('name', '').strip()
    if not name or len(name) > 100:
        messages.error(request, "Category name is required (100 characters max).")
    elif _category_name_taken(name):
        messages.error(request, f"A category named '{name}' already exists.")
    else:
        cat = Category.objects.create(name=name, description=request.POST.get('description', '').strip())
        log_audit(request, 'CREATE_CATEGORY', 'Category', cat.id, f"Added category '{name}'")
        messages.success(request, f"Category '{name}' added.")
    return redirect('system_settings')


@login_required
@admin_required
@require_POST
def category_update_view(request, category_id):
    cat = get_object_or_404(Category, id=category_id)
    name = request.POST.get('name', '').strip()
    if not name or len(name) > 100:
        messages.error(request, "Category name is required (100 characters max).")
    elif _category_name_taken(name, exclude_id=cat.id):
        messages.error(request, f"A category named '{name}' already exists.")
    else:
        old = cat.name
        cat.name = name
        cat.description = request.POST.get('description', '').strip()
        cat.save()
        log_audit(request, 'UPDATE_CATEGORY', 'Category', cat.id, f"Renamed/edited category '{old}' -> '{name}'")
        messages.success(request, f"Category '{name}' updated.")
    return redirect('system_settings')


@login_required
@admin_required
@require_POST
def category_delete_view(request, category_id):
    cat = get_object_or_404(Category, id=category_id)
    count = cat.books.count()
    if count:
        messages.error(request, f"Cannot delete '{cat.name}': {count} book(s) still use it. Move them to another category first.")
    else:
        name = cat.name
        cat.delete()
        log_audit(request, 'DELETE_CATEGORY', 'Category', category_id, f"Deleted category '{name}'")
        messages.success(request, f"Category '{name}' deleted.")
    return redirect('system_settings')


# ==========================================
# ADMIN: LIBRARIAN ACCOUNT MANAGEMENT
# ==========================================

def _librarian_or_404(user_id):
    """Only LIBRARIAN accounts can be managed here - never admins, never members."""
    return get_object_or_404(User.objects.select_related('profile'), pk=user_id, profile__role='LIBRARIAN')


def _password_problem(password, user=None):
    try:
        validate_password(password, user=user)
    except ValidationError as e:
        return " ".join(e.messages)
    return ''


@login_required
@admin_required
def librarian_list_view(request):
    librarians = (
        User.objects.filter(profile__role='LIBRARIAN').select_related('profile')
        .annotate(handled=Count('issued_transactions'))
        .order_by('-is_active', 'first_name', 'username')
    )
    return render(request, 'library_app/librarian_list.html', {'librarians': librarians})


@login_required
@admin_required
@require_POST
def librarian_create_view(request):
    username = request.POST.get('username', '').strip()
    password = request.POST.get('password', '')
    first_name = request.POST.get('first_name', '').strip()
    last_name = request.POST.get('last_name', '').strip()
    email = request.POST.get('email', '').strip()
    phone = request.POST.get('phone', '').strip()

    if not username or not first_name:
        messages.error(request, "Username and first name are required.")
    elif User.objects.filter(username__iexact=username).exists():
        messages.error(request, f"Username '{username}' is already taken.")
    elif email and User.objects.filter(email__iexact=email).exists():
        messages.error(request, f"Email '{email}' is already used by another account.")
    elif _password_problem(password, User(username=username, first_name=first_name, last_name=last_name, email=email)):
        messages.error(request, _password_problem(password, User(username=username, first_name=first_name, last_name=last_name, email=email)))
    else:
        user = User.objects.create_user(username=username, email=email, password=password,
                                        first_name=first_name, last_name=last_name)
        UserProfile.objects.create(user=user, role='LIBRARIAN', phone=phone)
        log_audit(request, 'CREATE_LIBRARIAN', 'User', user.id, f"Created librarian account '{username}'")
        messages.success(request, f"Librarian account '{username}' created.")
    return redirect('librarian_list')


@login_required
@admin_required
@require_POST
def librarian_update_view(request, user_id):
    target = _librarian_or_404(user_id)
    email = request.POST.get('email', '').strip()
    if email and User.objects.filter(email__iexact=email).exclude(pk=target.pk).exists():
        messages.error(request, f"Email '{email}' is already used by another account.")
        return redirect('librarian_list')
    first_name = request.POST.get('first_name', '').strip()
    if not first_name:
        messages.error(request, "First name is required.")
        return redirect('librarian_list')

    target.first_name = first_name
    target.last_name = request.POST.get('last_name', '').strip()
    target.email = email
    target.save()  # the role is deliberately never editable here
    target.profile.phone = request.POST.get('phone', '').strip()
    target.profile.save()
    log_audit(request, 'UPDATE_LIBRARIAN', 'User', target.id, f"Updated details of librarian '{target.username}'")
    messages.success(request, f"Account '{target.username}' updated.")
    return redirect('librarian_list')


@login_required
@admin_required
@require_POST
def librarian_toggle_active_view(request, user_id):
    target = _librarian_or_404(user_id)
    if target == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('librarian_list')
    target.is_active = not target.is_active
    target.save(update_fields=['is_active'])
    state = 'activated' if target.is_active else 'deactivated'
    log_audit(request, 'ACTIVATE_LIBRARIAN' if target.is_active else 'DEACTIVATE_LIBRARIAN', 'User', target.id,
              f"Librarian '{target.username}' {state}")
    messages.success(request, f"Account '{target.username}' {state}.")
    return redirect('librarian_list')


@login_required
@admin_required
@require_POST
def librarian_reset_password_view(request, user_id):
    target = _librarian_or_404(user_id)
    password = request.POST.get('password', '').strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(9)
    problem = _password_problem(password, target)
    if problem:
        messages.error(request, problem)
        return redirect('librarian_list')
    target.set_password(password)  # also signs the librarian out of any open session
    target.save()
    log_audit(request, 'RESET_LIBRARIAN_PASSWORD', 'User', target.id, f"Password reset for librarian '{target.username}'")  # never log the password
    if generated:
        messages.success(request, f"New temporary password for '{target.username}': {password}  (shown once - give it to them securely and ask them to keep it private.)")
    else:
        messages.success(request, f"Password for '{target.username}' was changed.")
    return redirect('librarian_list')


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
