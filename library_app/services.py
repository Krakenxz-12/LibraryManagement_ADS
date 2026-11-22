"""Borrowing workflow rules: requests, holds (24h pickup), reservation queue, weekly limit.

Stock rule used everywhere:
    Book.available_copies = copies on the shelf that are NOT on loan and NOT held for someone.
A copy is "held" while a BorrowRequest is APPROVED or a Reservation is READY. Holds are taken off
`available_copies` and given back when the hold is collected (it becomes a loan), cancelled or expired.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from .models import (
    ACTIVE_LOAN_STATUSES, SUSPENSION_LOCK_DAYS, SUSPENSION_STRIKES_FOR_SHORT_LOANS,
    AuditLog, Book, BookCopy, BorrowRequest, BorrowTransaction, BorrowingTier, LibrarySettings, Member, Notification, Reservation,
)
from .demand import demand_for_books, suggest_tier

BLOCKED_BOOK_STATUSES = ('LOST', 'UNDER_REPAIR')


class HoldError(Exception):
    """A borrowing rule was not met. The message is safe to show to the user."""


# ---------------------------------------------------------------- settings helpers
def hold_hours():
    return getattr(settings, 'PICKUP_HOLD_HOURS', 24)


def weekly_limit():
    return LibrarySettings.get().weekly_borrow_limit


def week_bounds(today=None):
    """Monday..Sunday of the current week (Asia/Manila)."""
    today = today or timezone.localdate()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def weekly_allowance(member):
    """Books the member has actually collected this week vs the weekly limit."""
    start, end = week_bounds()
    used = BorrowTransaction.objects.filter(member=member, issue_date__range=(start, end)).count()
    limit = weekly_limit()
    return {'limit': limit, 'used': used, 'remaining': max(limit - used, 0), 'week_start': start, 'week_end': end}


# ---------------------------------------------------------------- notifications
def _fmt(dt):
    return timezone.localtime(dt).strftime('%b %d, %I:%M %p')


def _notify(user, title, message, kind='GENERAL'):
    if user:
        Notification.objects.create(recipient=user, title=title, message=message, notification_type=kind)


def _notify_staff(title, message):
    for staff in User.objects.filter(profile__role__in=['ADMIN', 'LIBRARIAN']):
        _notify(staff, title, message, 'LIBRARIAN_ALERT')


# ---------------------------------------------------------------- overdue suspensions
# Policy (constants live in models.py):
#  * The day after a loan's due date, a member with an unreturned book is suspended automatically.
#  * They are reactivated automatically once every overdue book is back (and any lock has passed).
#  * Each automatic suspension is a "strike"; returning a book on time (with nothing else overdue) resets them.
#  * From the 3rd consecutive strike, new loans only get the shortest active borrowing period.
#  * Beyond that (4th strike onwards) the suspension also lasts at least a week, even after returning the book.
#  * A librarian's manual status change always wins: manual suspensions are never auto-reactivated, and
#    loans that already caused a strike never cause a second one.
def shortest_loan_days():
    tier = BorrowingTier.objects.filter(is_active=True).order_by('days', 'id').first()
    return tier.days if tier else None


def suspension_message(member):
    """Why the member is suspended (safe to show to them), or '' if it was not an automatic suspension."""
    if member.status != 'SUSPENDED' or not member.auto_suspended:
        return ''
    msg = "Your membership is suspended because you have an unreturned overdue book. Return it to be reactivated automatically."
    if member.suspended_until and member.suspended_until > timezone.now():
        msg += f" Because of repeated overdue suspensions, it stays suspended until {timezone.localtime(member.suspended_until).strftime('%b %d, %Y %I:%M %p')}."
    return msg


def _log_member_event(action, member, details):
    AuditLog.objects.create(user=None, action=action, model_name='Member', object_id=str(member.pk), details=details)


def refresh_member_standing(member, now=None):
    """Apply the overdue suspension rules to one member and update the passed instance.
    Returns 'suspended', 'reactivated' or None (nothing changed)."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    outcome = None
    with transaction.atomic():
        m = Member.objects.select_for_update().get(pk=member.pk)
        overdue = BorrowTransaction.objects.filter(member=m, status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today)
        if m.status == 'ACTIVE':
            if overdue.filter(suspension_counted=False).exists():
                m.suspension_strikes += 1
                m.status, m.auto_suspended = 'SUSPENDED', True
                locked = m.suspension_strikes > SUSPENSION_STRIKES_FOR_SHORT_LOANS
                m.suspended_until = now + timedelta(days=SUSPENSION_LOCK_DAYS) if locked else None
                m.save(update_fields=['suspension_strikes', 'status', 'auto_suspended', 'suspended_until', 'updated_at'])
                overdue.update(suspension_counted=True)
                outcome = 'suspended'
                msg = "Your membership was suspended because a borrowed book is overdue. Return it and you will be reactivated automatically."
                if locked:
                    msg += f" This is suspension #{m.suspension_strikes} in a row, so you stay suspended for at least {SUSPENSION_LOCK_DAYS} days even after returning it."
                elif m.suspension_strikes == SUSPENSION_STRIKES_FOR_SHORT_LOANS:
                    msg += " This is your 3rd in a row: your future loans will only get the shortest borrowing period, and further suspensions will last a week."
                _log_member_event('AUTO_SUSPEND', m, f"{m.member_id} suspended automatically for overdue book(s) (strike {m.suspension_strikes}).")
                if m.user_id:
                    _notify(m.user, "Membership suspended", msg, 'OVERDUE')
                _notify_staff("Member suspended automatically", f"{m.full_name} ({m.member_id}) was suspended for an unreturned book (strike {m.suspension_strikes}).")
        elif m.status == 'SUSPENDED' and m.auto_suspended:
            if not overdue.exists() and (m.suspended_until is None or m.suspended_until <= now):
                m.status, m.auto_suspended, m.suspended_until = 'ACTIVE', False, None
                m.save(update_fields=['status', 'auto_suspended', 'suspended_until', 'updated_at'])
                outcome = 'reactivated'
                _log_member_event('AUTO_REACTIVATE', m, f"{m.member_id} reactivated automatically (all overdue books returned).")
                if m.user_id:
                    _notify(m.user, "Membership reactivated", "Thanks for returning your book. Your membership is active again.", 'GENERAL')
    member.refresh_from_db()
    return outcome


def sync_suspensions(now=None):
    """Check every member who might need suspending or reactivating. Safe to run as often as you like."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    ids = set(BorrowTransaction.objects.filter(
        status__in=ACTIVE_LOAN_STATUSES, due_date__lt=today, suspension_counted=False, member__status='ACTIVE',
    ).values_list('member_id', flat=True))
    ids |= set(Member.objects.filter(status='SUSPENDED', auto_suspended=True).values_list('id', flat=True))
    changed = 0
    for member in Member.objects.filter(id__in=ids):
        if refresh_member_standing(member, now):
            changed += 1
    return changed


def on_loan_returned(txn):
    """Call after a loan is returned: a clean on-time return ends the strike streak, then re-check the member."""
    with transaction.atomic():
        m = Member.objects.select_for_update().get(pk=txn.member_id)
        on_time = bool(txn.return_date and txn.return_date <= txn.due_date)
        still_overdue = BorrowTransaction.objects.filter(
            member=m, status__in=ACTIVE_LOAN_STATUSES, due_date__lt=timezone.localdate()).exists()
        if on_time and not still_overdue and m.suspension_strikes:
            had_short_loans = m.has_shortened_loans
            m.suspension_strikes = 0
            m.save(update_fields=['suspension_strikes', 'updated_at'])
            if had_short_loans and m.user_id:
                _notify(m.user, "Borrowing period restored", "Thanks for returning on time. Your borrowing periods are back to normal.", 'GENERAL')
    refresh_member_standing(txn.member)


# ---------------------------------------------------------------- named copies
# Every physical copy has its own record (BookCopy). Only IN_CIRCULATION copies can be lent; a copy that is
# Under Repair or Lost leaves the shelf, so the book's available count drops and it cannot be borrowed.
COPY_STATUS_LABELS = dict(BookCopy.STATUS_CHOICES)


def pick_free_copy(book):
    """A copy that is in good condition and not currently on loan (lowest copy number first)."""
    on_loan = BorrowTransaction.objects.filter(
        book=book, status__in=ACTIVE_LOAN_STATUSES, copy__isnull=False).values('copy_id')
    return (BookCopy.objects.filter(book=book, status='IN_CIRCULATION')
            .exclude(id__in=on_loan).order_by('copy_number').first())


def return_copy_to_shelf(book, copy):
    """A loaned copy came back. It only becomes available again if it is still in good condition."""
    if copy is None or copy.status == 'IN_CIRCULATION':
        book.available_copies = min(book.available_copies + 1, book.total_copies)
        book.save(update_fields=['available_copies'])
        return True
    return False


def _bump_hold(book):
    """A shelf copy was withdrawn but every remaining shelf copy is already held for someone.
    Release the most recent hold: a reservation goes back to the queue, a request is cancelled."""
    res = book.reservations.filter(status='READY').select_related('member__user').order_by('-notified_at', '-id').first()
    if res:
        res.status, res.notified_at, res.pickup_deadline = 'PENDING', None, None
        res.save(update_fields=['status', 'notified_at', 'pickup_deadline'])
        if res.member.user_id:
            _notify(res.member.user, f"Reservation update: '{book.title}'",
                    f"The copy held for you was withdrawn from circulation. You are still in the queue and will be notified when another copy is ready.",
                    'RESERVATION_READY')
        return True
    req = book.borrow_requests.filter(status='APPROVED').select_related('member__user').order_by('-handled_at', '-id').first()
    if req:
        req.status = 'CANCELLED'
        req.save(update_fields=['status'])
        if req.member.user_id:
            _notify(req.member.user, f"Request cancelled: '{book.title}'",
                    "The copy set aside for you was withdrawn from circulation. Please submit a new request or reserve the book.")
        return True
    return False


def set_copy_status(copy_id, new_status, user=None, note=''):
    """Change a copy's condition (In Circulation / Under Repair / Lost) and keep the book's counts in step.
    Returns the updated copy."""
    if new_status not in COPY_STATUS_LABELS:
        raise HoldError("Unknown copy status.")
    with transaction.atomic():
        copy = BookCopy.objects.select_for_update().get(pk=copy_id)
        book = Book.objects.select_for_update().get(pk=copy.book_id)
        old = copy.status
        note = (note or '').strip()
        if old == new_status:
            if note != copy.notes:
                copy.notes = note
                copy.save(update_fields=['notes'])
            return copy
        on_loan = BorrowTransaction.objects.filter(copy=copy, status__in=ACTIVE_LOAN_STATUSES).exists()
        copy.status, copy.notes, copy.status_changed_at = new_status, note, timezone.now()
        copy.save(update_fields=['status', 'notes', 'status_changed_at'])
        if not on_loan:  # a copy that is out on loan is not on the shelf, so the shelf count does not change yet
            if old == 'IN_CIRCULATION':
                if book.available_copies > 0:
                    book.available_copies -= 1
                else:
                    _bump_hold(book)
            elif new_status == 'IN_CIRCULATION':
                book.available_copies = min(book.available_copies + 1, book.total_copies)
        book.update_availability_status()  # Lost / Under Repair / Available ... from the copies
        promote_reservations(book)
        AuditLog.objects.create(
            user=user, action='COPY_STATUS', model_name='BookCopy', object_id=str(copy.pk),
            details=f"{copy.copy_code} ({book.title}): {COPY_STATUS_LABELS[old]} -> {COPY_STATUS_LABELS[new_status]}"
                    + (f". Note: {note}" if note else '') + (" (currently on loan)" if on_loan else ''),
        )
    return copy


def add_copy(book_id, user=None):
    """Register one more physical copy of a book; it goes straight onto the shelf."""
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book_id)
        number = (book.copies.order_by('-copy_number').values_list('copy_number', flat=True).first() or 0) + 1
        copy = BookCopy.objects.create(book=book, copy_number=number, copy_code=BookCopy.make_code(book, number))
        book.total_copies = book.copies.count()
        book.available_copies += 1
        book.save(update_fields=['total_copies', 'available_copies'])
        book.update_availability_status()
        promote_reservations(book)
        AuditLog.objects.create(user=user, action='COPY_ADDED', model_name='BookCopy', object_id=str(copy.pk),
                                details=f"Added {copy.copy_code} to '{book.title}'.")
    return copy


# ---------------------------------------------------------------- stock helpers
def _create_loan(member, book, user, days=None, tier=None):
    """Start a borrow NOW and store the suggested duration on the transaction.

    The suggestion is recalculated at checkout so the saved due date matches the
    current demand. Explicit days remain supported for existing service callers.
    """
    rules = LibrarySettings.get()
    demand = demand_for_books([book])[book.id]
    active_tiers = list(BorrowingTier.objects.filter(is_active=True).order_by('days', 'id'))
    suggested = suggest_tier(demand, active_tiers)
    terms = tier or suggested or book.resolve_loan_terms()
    if tier is not None:
        days = tier.days
    elif days is None:
        days = terms.days
    copy = pick_free_copy(book)
    if copy is None:
        raise HoldError(f"No copy of '{book.title}' is on the shelf in good condition right now.")
    issue_date = timezone.localdate()
    tier_name = terms.name if tier is not None or days == terms.days else (next((item.name for item in active_tiers if item.days == days), None) or f'{days}-day requested')
    shortest = shortest_loan_days() if member.has_shortened_loans else None
    if shortest is not None and days > shortest:  # repeat overdue offenders only get the shortest period
        days, tier_name = shortest, f'Restricted ({shortest} days)'
    return BorrowTransaction.objects.create(
        transaction_code=BorrowTransaction.generate_transaction_code(),
        member=member, book=book, copy=copy, issued_by=user,
        issue_date=issue_date, due_date=issue_date + timedelta(days=days),
        loan_days=days, tier_name=tier_name,
        daily_fine_rate=rules.daily_fine_rate,
        status='BORROWED',
    )


def _check_can_collect(member, book):
    refresh_member_standing(member)
    if member.status != 'ACTIVE':
        raise HoldError(f"Member {member.member_id} is inactive or suspended.")
    if book.status in BLOCKED_BOOK_STATUSES:
        raise HoldError(f"'{book.title}' is marked {book.get_status_display()} and cannot be borrowed.")
    allowance = weekly_allowance(member)
    if allowance['remaining'] <= 0:
        raise HoldError(
            f"{member.full_name} has already collected {allowance['limit']} books this week "
            f"(limit resets Monday)."
        )


def promote_reservations(book):
    """Hand free copies to waiting reservations (first come, first served), each with a pickup hold.
    Call inside a transaction with the book row locked. Returns the promoted reservations."""
    promoted = []
    while book.available_copies > 0 and book.status not in BLOCKED_BOOK_STATUSES:
        res = (
            book.reservations.filter(status='PENDING')
            .select_related('member__user').order_by('reservation_date', 'id').first()
        )
        if not res:
            break
        now = timezone.now()
        res.status = 'READY'
        res.notified_at = now
        res.pickup_deadline = now + timedelta(hours=hold_hours())
        res.save(update_fields=['status', 'notified_at', 'pickup_deadline'])
        book.available_copies -= 1
        _notify(
            res.member.user, f"Reserved Book Ready: '{book.title}'",
            f"'{book.title}' is being held for you at shelf #{book.shelf_number}. "
            f"Please collect it from the librarian before {_fmt(res.pickup_deadline)}, or the hold expires. "
            f"Your borrowing period will be determined by the librarian when you collect it.",
            'RESERVATION_READY',
        )
        promoted.append(res)
    book.update_availability_status()  # saves status + available_copies
    return promoted


def _release_copy(book):
    """Give a held copy back to the shelf, then offer it to the next waiting reservation."""
    book.available_copies = min(book.available_copies + 1, book.total_copies)
    book.save(update_fields=['available_copies'])
    return promote_reservations(book)


# ---------------------------------------------------------------- borrow requests
def submit_borrow_request(member, book):
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book.pk)
        refresh_member_standing(member)
        if member.status != 'ACTIVE':
            raise HoldError(suspension_message(member) or "Your membership is not active. Please see the librarian.")
        if book.status in BLOCKED_BOOK_STATUSES:
            raise HoldError("This book is not available for borrowing right now.")
        if book.available_copies <= 0:
            raise HoldError("No copy is available right now. You can reserve this book instead.")
        if BorrowRequest.objects.filter(member=member, book=book, status__in=['PENDING', 'APPROVED']).exists():
            raise HoldError("You already have an open request for this book.")
        if BorrowTransaction.objects.filter(member=member, book=book, status__in=ACTIVE_LOAN_STATUSES).exists():
            raise HoldError("You are currently borrowing this book.")
        demand = demand_for_books([book])[book.id]
        active_tiers = list(BorrowingTier.objects.filter(is_active=True).order_by('days', 'id'))
        suggested = suggest_tier(demand, active_tiers)
        # The suggested tier is only a starting point for the librarian's decision.
        initial_tier = suggested or book.resolve_loan_terms().tier
        req = BorrowRequest.objects.create(
            member=member, book=book,
            requested_days=(initial_tier.days if initial_tier else book.resolve_loan_terms().days),
        )
    _notify_staff(
        f"New Borrow Request: '{book.title}'",
        f"{member.full_name} ({member.member_id}) requested to borrow '{book.title}'. Review it under Borrow Requests.",
    )
    return req


def approve_request(request_id, user, tier_id=None):
    """Librarian approves: the copy is held and the member has PICKUP_HOLD_HOURS to collect it."""
    with transaction.atomic():
        req = BorrowRequest.objects.select_for_update().select_related('member__user').get(pk=request_id)
        book = Book.objects.select_for_update().get(pk=req.book_id)
        if req.status != 'PENDING':
            raise HoldError("This request has already been handled.")
        if req.member.status != 'ACTIVE':
            raise HoldError("This member is inactive or suspended.")
        if book.status in BLOCKED_BOOK_STATUSES:
            raise HoldError(f"'{book.title}' is marked {book.get_status_display()}.")
        if book.available_copies <= 0:
            raise HoldError("No free copy left to hold. Reject this request and let the member reserve instead.")
        selected_tier = BorrowingTier.objects.filter(pk=tier_id, is_active=True).first() if tier_id else None
        if selected_tier is None:
            raise HoldError("Choose an active borrowing tier before approving this request.")
        req.borrowing_tier = selected_tier
        req.requested_days = selected_tier.days
        now = timezone.now()
        book.available_copies -= 1
        req.status = 'APPROVED'
        req.handled_by = user
        req.handled_at = now
        req.pickup_deadline = now + timedelta(hours=hold_hours())
        req.save()
        book.update_availability_status()
    _notify(
        req.member.user, f"Request Approved: '{book.title}'",
        f"Your request was approved. Collect '{book.title}' (shelf #{book.shelf_number}) from the librarian "
        f"before {_fmt(req.pickup_deadline)}, or it will be released. "
        f"The librarian assigned the {req.borrowing_tier.name} tier ({req.requested_days} days). Your borrowing period starts when you collect it.",
    )
    return req


def reject_request(request_id, user, reason=''):
    with transaction.atomic():
        req = BorrowRequest.objects.select_for_update().select_related('member__user', 'book').get(pk=request_id)
        if req.status != 'PENDING':
            raise HoldError("This request has already been handled.")
        req.status = 'REJECTED'
        req.handled_by = user
        req.handled_at = timezone.now()
        req.reject_reason = reason.strip()
        req.save()
    _notify(req.member.user, f"Request Declined: '{req.book.title}'",
            f"Your request to borrow '{req.book.title}' was declined." + (f" Reason: {req.reject_reason}" if req.reject_reason else ''))
    return req


def cancel_request(request_id, user):
    """Cancel a PENDING or APPROVED request. An approved one gives its held copy back."""
    with transaction.atomic():
        req = BorrowRequest.objects.select_for_update().select_related('member__user').get(pk=request_id)
        book = Book.objects.select_for_update().get(pk=req.book_id)
        if req.status not in ('PENDING', 'APPROVED'):
            raise HoldError("This request can no longer be cancelled.")
        was_held = req.status == 'APPROVED'
        req.status = 'CANCELLED'
        req.handled_by = req.handled_by or user
        req.save()
        if was_held:
            _release_copy(book)
    return req


def confirm_pickup(request_id, user, days=None):
    """Librarian confirms the member physically received the book -> loan starts NOW."""
    with transaction.atomic():
        req = BorrowRequest.objects.select_for_update().select_related('member').get(pk=request_id)
        book = Book.objects.select_for_update().get(pk=req.book_id)
        if req.status != 'APPROVED':
            raise HoldError("This request is not awaiting pickup (it may have expired or been cancelled).")
        if req.pickup_deadline and req.pickup_deadline < timezone.now():
            raise HoldError("The pickup window has passed. The hold will be released automatically.")
        _check_can_collect(req.member, book)
        txn = _create_loan(req.member, book, user, req.requested_days if days is None else days, tier=req.borrowing_tier)
        req.status = 'COLLECTED'
        req.collected_at = timezone.now()
        req.transaction = txn
        req.save()
        book.update_availability_status()  # the copy was already held, so stock does not change
    return txn


# ---------------------------------------------------------------- reservations
def place_reservation(member, book):
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book.pk)
        refresh_member_standing(member)
        if member.status != 'ACTIVE':
            raise HoldError(suspension_message(member) or "This membership is not active.")
        if book.status in BLOCKED_BOOK_STATUSES:
            raise HoldError(f"'{book.title}' is not available for reservation right now.")
        if book.available_copies > 0:
            raise HoldError("A copy is available right now, so request to borrow it instead of reserving.")
        if Reservation.objects.filter(book=book, member=member, status__in=['PENDING', 'READY']).exists():
            raise HoldError(f"{member.full_name} already has an active reservation for '{book.title}'.")
        if BorrowTransaction.objects.filter(member=member, book=book, status__in=ACTIVE_LOAN_STATUSES).exists():
            raise HoldError("This member is currently borrowing this book.")
        res = Reservation.objects.create(book=book, member=member, status='PENDING')
        book.update_availability_status()
    return res


def cancel_reservation(reservation_id):
    with transaction.atomic():
        res = Reservation.objects.select_for_update().get(pk=reservation_id)
        book = Book.objects.select_for_update().get(pk=res.book_id)
        if res.status not in ('PENDING', 'READY'):
            raise HoldError("This reservation can no longer be cancelled.")
        was_held = res.status == 'READY'
        res.status = 'CANCELLED'
        res.save(update_fields=['status'])
        if was_held:
            _release_copy(book)
        else:
            book.update_availability_status()
    return res


def confirm_reservation_pickup(reservation_id, user, days=None):
    with transaction.atomic():
        res = Reservation.objects.select_for_update().select_related('member').get(pk=reservation_id)
        book = Book.objects.select_for_update().get(pk=res.book_id)
        if res.status != 'READY':
            raise HoldError("This reservation is not ready for pickup (it may have expired or been cancelled).")
        if res.pickup_deadline and res.pickup_deadline < timezone.now():
            raise HoldError("The pickup window has passed. The hold will be released automatically.")
        _check_can_collect(res.member, book)
        txn = _create_loan(res.member, book, user, days)
        res.status = 'FULFILLED'
        res.save(update_fields=['status'])
        book.update_availability_status()
    return txn


# ---------------------------------------------------------------- direct desk checkout
def checkout_at_desk(member, book_id, user, days=None, tier_id=None):
    """Walk-in checkout by a librarian. Uses the member's own hold if they have one."""
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book_id)
        selected_tier = None
        if tier_id:
            selected_tier = BorrowingTier.objects.filter(pk=tier_id, is_active=True).first()
            if selected_tier is None:
                raise HoldError("Please select a valid active borrowing tier.")
        _check_can_collect(member, book)
        hold_req = BorrowRequest.objects.select_for_update().filter(member=member, book=book, status='APPROVED').first()
        hold_res = Reservation.objects.select_for_update().filter(member=member, book=book, status='READY').first()
        if not (hold_req or hold_res):
            if book.available_copies <= 0:
                raise HoldError(f"Book '{book.title}' currently has no copies available for checkout.")
            book.available_copies -= 1
        txn = _create_loan(member, book, user, days, tier=selected_tier)
        if hold_req:
            hold_req.status, hold_req.collected_at, hold_req.transaction = 'COLLECTED', timezone.now(), txn
            hold_req.save()
        for res in Reservation.objects.filter(member=member, book=book, status__in=['PENDING', 'READY']):
            res.status = 'FULFILLED'
            res.save(update_fields=['status'])
        book.update_availability_status()
    return txn


# ---------------------------------------------------------------- renewals
def renewal_block_reason(txn, today=None):
    """Why this loan cannot be renewed right now (a message safe to show), or None if it can."""
    today = today or timezone.localdate()
    max_renewals = LibrarySettings.get().max_renewals
    if txn.status == 'RETURNED':
        return "This book has already been returned."
    if max_renewals <= 0:
        return "Renewals are turned off."
    if txn.status == 'OVERDUE' or txn.due_date < today:
        return "Overdue books cannot be renewed. Please return it."
    if txn.renewal_count >= max_renewals:
        return "Renewal limit reached for this loan." if max_renewals > 1 else "Already renewed once."
    member = txn.member
    if member.status != 'ACTIVE':
        return "Membership is not active."
    if member.balance > 0:
        return "Unpaid fines must be settled first."
    if txn.book.reservations.filter(status__in=['PENDING', 'READY']).exists():
        return "Another member has reserved this book."
    return None


def renew_loan(transaction_id):
    """Extend a loan by its own original length, counted from the current due date."""
    with transaction.atomic():
        txn = BorrowTransaction.objects.select_for_update().select_related('member__user').get(pk=transaction_id)
        book = Book.objects.select_for_update().get(pk=txn.book_id)  # same lock reservations take
        txn.book = book
        reason = renewal_block_reason(txn)
        if reason:
            raise HoldError(reason)
        txn.due_date += timedelta(days=txn.loan_days or 14)
        txn.renewal_count += 1
        txn.save(update_fields=['due_date', 'renewal_count', 'updated_at'])
    _notify(
        txn.member.user, f"Loan Renewed: '{book.title}'",
        f"'{book.title}' is now due on {txn.due_date.strftime('%b %d, %Y')}.",
    )
    return txn


# ---------------------------------------------------------------- expiry
def expire_stale_holds(now=None):
    """Release every hold that was not collected in time. Safe to call as often as you like."""
    now = now or timezone.now()
    expired = 0

    for pk in BorrowRequest.objects.filter(status='APPROVED', pickup_deadline__lt=now).values_list('pk', flat=True):
        with transaction.atomic():
            req = BorrowRequest.objects.select_for_update().select_related('member__user', 'book').get(pk=pk)
            if req.status != 'APPROVED':
                continue
            book = Book.objects.select_for_update().get(pk=req.book_id)
            req.status = 'EXPIRED'
            req.save(update_fields=['status'])
            _release_copy(book)
        _notify(req.member.user, f"Request Expired: '{req.book.title}'",
                f"You did not collect '{req.book.title}' within {hold_hours()} hours, so the hold was released.")
        expired += 1

    for pk in Reservation.objects.filter(status='READY', pickup_deadline__lt=now).values_list('pk', flat=True):
        with transaction.atomic():
            res = Reservation.objects.select_for_update().select_related('member__user', 'book').get(pk=pk)
            if res.status != 'READY':
                continue
            book = Book.objects.select_for_update().get(pk=res.book_id)
            res.status = 'EXPIRED'
            res.save(update_fields=['status'])
            _release_copy(book)  # next person in the queue gets their turn
        _notify(res.member.user, f"Reservation Expired: '{res.book.title}'",
                f"You did not collect '{res.book.title}' within {hold_hours()} hours, so your reservation expired.")
        expired += 1

    return expired
