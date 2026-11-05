"""Book demand status and borrowing-tier suggestions.

Demand is worked out from two signals:
  * how often the book was borrowed recently (loans in the last DEMAND_WINDOW_DAYS, per copy), and
  * how many people are waiting for it right now (reservations + borrow requests).

The result is only a SUGGESTION. Nothing changes until an Admin applies it.
"""
from collections import namedtuple
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from .models import Book, BorrowingTier, BorrowRequest, BorrowTransaction, Reservation

DEMAND_WINDOW_DAYS = 90       # how far back borrowing history is counted
NEW_BOOK_DAYS = 30            # books added this recently count as "newly acquired"
HIGH_LOANS_PER_COPY = 4       # this many recent loans per copy = high demand
LOW_LOANS_PER_COPY = 1        # fewer recent loans per copy (and nobody waiting) = low demand
HIGH_WAITING = 2              # this many people waiting at once = high demand

HIGH, NORMAL, LOW = 'HIGH', 'NORMAL', 'LOW'
LEVEL_LABELS = {HIGH: 'High demand', NORMAL: 'Normal demand', LOW: 'Low demand'}

Demand = namedtuple('Demand', 'level label is_new loans waiting queued reason')


def _counts(model, book_ids, **filters):
    rows = model.objects.filter(book_id__in=book_ids, **filters).values('book_id').annotate(n=Count('id'))
    return {r['book_id']: r['n'] for r in rows}


def demand_for_books(books):
    """{book.id: Demand} for the given books, using three grouped queries (no per-book queries)."""
    books = list(books)
    ids = [b.id for b in books]
    if not ids:
        return {}
    now = timezone.now()
    since = timezone.localdate() - timedelta(days=DEMAND_WINDOW_DAYS)
    loans = _counts(BorrowTransaction, ids, issue_date__gte=since)
    queued = _counts(Reservation, ids, status__in=['PENDING', 'READY'])
    requested = _counts(BorrowRequest, ids, status__in=['PENDING', 'APPROVED'])

    result = {}
    for b in books:
        n_loans, n_queued = loans.get(b.id, 0), queued.get(b.id, 0)
        waiting = n_queued + requested.get(b.id, 0)
        per_copy = n_loans / max(b.total_copies, 1)
        is_new = b.created_at >= now - timedelta(days=NEW_BOOK_DAYS)

        if n_queued >= 1 or waiting >= HIGH_WAITING or per_copy >= HIGH_LOANS_PER_COPY:
            level = HIGH
            reason = (f"{waiting} waiting now" if waiting >= HIGH_WAITING or n_queued
                      else f"{n_loans} loans in {DEMAND_WINDOW_DAYS} days for {b.total_copies} copy(ies)")
        elif per_copy < LOW_LOANS_PER_COPY and waiting == 0:
            level, reason = LOW, f"{n_loans} loan(s) in the last {DEMAND_WINDOW_DAYS} days, nobody waiting"
        else:
            level, reason = NORMAL, f"{n_loans} loan(s) in the last {DEMAND_WINDOW_DAYS} days"
        result[b.id] = Demand(level, LEVEL_LABELS[level], is_new, n_loans, waiting, n_queued, reason)
    return result


def suggest_tier(demand, tiers=None):
    """High demand or newly acquired -> shortest active tier; low demand -> longest; otherwise the default.
    Returns None when there is nothing sensible to suggest (e.g. no active tiers)."""
    tiers = tiers if tiers is not None else list(BorrowingTier.objects.filter(is_active=True).order_by('days', 'id'))
    if not tiers:
        return None
    if demand.level == HIGH or demand.is_new:
        return tiers[0]
    if demand.level == LOW:
        return tiers[-1]
    return next((t for t in tiers if t.is_default), None) or tiers[0]


def tier_suggestions(books=None):
    """Rows for books whose suggested tier differs from the one they use today:
    [{'book', 'demand', 'current' (LoanTerms), 'suggested' (BorrowingTier)}], highest demand first."""
    books = list(books if books is not None else
                 Book.objects.exclude(status__in=['LOST', 'UNDER_REPAIR'])
                 .select_related('category__borrowing_tier', 'borrowing_tier'))
    tiers = list(BorrowingTier.objects.filter(is_active=True).order_by('days', 'id'))
    if len(tiers) < 2:
        return []
    demand = demand_for_books(books)
    rows = []
    for b in books:
        d = demand[b.id]
        suggested, current = suggest_tier(d, tiers), b.resolve_loan_terms()
        if suggested and suggested.days != current.days:
            rows.append({'book': b, 'demand': d, 'current': current, 'suggested': suggested})
    order = {HIGH: 0, NORMAL: 1, LOW: 2}
    rows.sort(key=lambda r: (order[r['demand'].level], r['book'].title.lower()))
    return rows
