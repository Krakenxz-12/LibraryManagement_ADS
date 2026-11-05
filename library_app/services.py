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
    ACTIVE_LOAN_STATUSES, Book, BorrowRequest, BorrowTransaction, LibrarySettings, Notification, Reservation,
)

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


# ---------------------------------------------------------------- stock helpers
def _create_loan(member, book, user, days=None):
    rules = LibrarySettings.get()
    days = days or rules.borrow_period_days
    issue_date = timezone.localdate()
    return BorrowTransaction.objects.create(
        transaction_code=BorrowTransaction.generate_transaction_code(),
        member=member, book=book, issued_by=user,
        issue_date=issue_date, due_date=issue_date + timedelta(days=days),
        daily_fine_rate=rules.daily_fine_rate,
        status='BORROWED',
    )


def _check_can_collect(member, book):
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
            f"Please collect it from the librarian before {_fmt(res.pickup_deadline)}, or the hold expires.",
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
        if member.status != 'ACTIVE':
            raise HoldError("Your membership is not active. Please see the librarian.")
        if book.status in BLOCKED_BOOK_STATUSES:
            raise HoldError("This book is not available for borrowing right now.")
        if book.available_copies <= 0:
            raise HoldError("No copy is available right now. You can reserve this book instead.")
        if BorrowRequest.objects.filter(member=member, book=book, status__in=['PENDING', 'APPROVED']).exists():
            raise HoldError("You already have an open request for this book.")
        if BorrowTransaction.objects.filter(member=member, book=book, status__in=ACTIVE_LOAN_STATUSES).exists():
            raise HoldError("You are currently borrowing this book.")
        req = BorrowRequest.objects.create(member=member, book=book)
    _notify_staff(
        f"New Borrow Request: '{book.title}'",
        f"{member.full_name} ({member.member_id}) requested to borrow '{book.title}'. Review it under Borrow Requests.",
    )
    return req


def approve_request(request_id, user):
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
        f"before {_fmt(req.pickup_deadline)}, or it will be released.",
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
        txn = _create_loan(req.member, book, user, days)
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
        if member.status != 'ACTIVE':
            raise HoldError("This membership is not active.")
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
def checkout_at_desk(member, book_id, user, days=None):
    """Walk-in checkout by a librarian. Uses the member's own hold if they have one."""
    with transaction.atomic():
        book = Book.objects.select_for_update().get(pk=book_id)
        _check_can_collect(member, book)
        hold_req = BorrowRequest.objects.select_for_update().filter(member=member, book=book, status='APPROVED').first()
        hold_res = Reservation.objects.select_for_update().filter(member=member, book=book, status='READY').first()
        if not (hold_req or hold_res):
            if book.available_copies <= 0:
                raise HoldError(f"Book '{book.title}' currently has no copies available for checkout.")
            book.available_copies -= 1
        txn = _create_loan(member, book, user, days)
        if hold_req:
            hold_req.status, hold_req.collected_at, hold_req.transaction = 'COLLECTED', timezone.now(), txn
            hold_req.save()
        for res in Reservation.objects.filter(member=member, book=book, status__in=['PENDING', 'READY']):
            res.status = 'FULFILLED'
            res.save(update_fields=['status'])
        book.update_availability_status()
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
