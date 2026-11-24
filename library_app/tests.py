from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import (
    UserProfile, Category, Member, Book, BorrowTransaction, FinePayment, Notification, BookReview, AuditLog, BookCopy, Reservation,
)
from .utils import check_and_create_alerts


class LibraryFlowTests(TestCase):
    def setUp(self):
        self.librarian = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=self.librarian, role='LIBRARIAN')
        self.client = Client()
        self.client.force_login(self.librarian)
        cat = Category.objects.create(name='Fiction')
        self.book = Book.objects.create(
            title='T', author='A', isbn='111', category=cat,
            shelf_number='S1', total_copies=1, available_copies=1,
        )
        self.member = Member.objects.create(
            member_id='MEM-2026-0001', first_name='Ana', last_name='Cruz',
            email='ana@example.com', phone='0917',
        )

    def borrow(self):
        return self.client.post('/transactions/borrow/', {
            'member_id': self.member.id, 'book_id': self.book.id, 'borrow_days': 14,
        })

    def test_borrow_decrements_copies_and_blocks_second_checkout(self):
        self.borrow()
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 0)
        self.assertEqual(self.book.status, 'BORROWED')
        self.borrow()  # no copies left
        self.assertEqual(BorrowTransaction.objects.count(), 1)

    def test_return_restores_availability(self):
        self.borrow()
        txn = BorrowTransaction.objects.get()
        self.client.get(f'/transactions/{txn.id}/return/')
        self.book.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)
        self.assertEqual(self.book.status, 'AVAILABLE')
        self.assertEqual(txn.status, 'RETURNED')

    def test_overdue_loan_stays_visible_and_fine_is_charged_on_return(self):
        self.borrow()
        txn = BorrowTransaction.objects.get()
        today = timezone.localdate()
        txn.issue_date = today - timedelta(days=20)
        txn.due_date = today - timedelta(days=5)
        txn.save()

        check_and_create_alerts()
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'OVERDUE')
        self.assertTrue(txn.is_currently_overdue)
        self.assertEqual(self.member.overdue_borrows_count, 1)
        self.assertTrue(
            Notification.objects.filter(recipient=self.librarian, notification_type='LIBRARIAN_ALERT').exists()
        )

        self.client.get(f'/transactions/{txn.id}/return/')
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'RETURNED')
        self.assertEqual(txn.fine_amount, Decimal('100.00'))  # 5 days x PHP 20

    def test_cannot_delete_book_with_history(self):
        self.borrow()
        response = self.client.get(f'/books/{self.book.id}/delete/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_fine_payment_cannot_exceed_outstanding_and_marks_paid(self):
        self.borrow()
        txn = BorrowTransaction.objects.get()
        txn.fine_amount = Decimal('100.00')
        txn.save()
        pay = f'/transactions/{txn.id}/pay-fine/'
        self.client.post(pay, {'amount': '150.00', 'payment_method': 'CASH'})
        self.assertEqual(FinePayment.objects.count(), 0)
        self.client.post(pay, {'amount': '40.00', 'payment_method': 'CASH'})
        txn.refresh_from_db()
        self.assertFalse(txn.fine_paid)
        self.client.post(pay, {'amount': '60.00', 'payment_method': 'CASH'})
        txn.refresh_from_db()
        self.assertTrue(txn.fine_paid)


class MemberFinesPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('stud', password='pw')
        UserProfile.objects.create(user=self.user, role='MEMBER')
        self.member = Member.objects.create(
            user=self.user, member_id='MEM-2026-0002', first_name='Ben', last_name='Reyes',
            email='ben@example.com', phone='0918',
        )
        staff = User.objects.create_user('lib2', password='pw')
        UserProfile.objects.create(user=staff, role='LIBRARIAN')
        self.staff = staff
        cat = Category.objects.create(name='Sci')
        book = Book.objects.create(title='B', author='A', isbn='222', category=cat,
                                   shelf_number='S2', total_copies=1, available_copies=1)
        today = timezone.localdate()
        self.txn = BorrowTransaction.objects.create(
            transaction_code='TXN-T-1', member=self.member, book=book, issued_by=staff,
            issue_date=today - timedelta(days=20), due_date=today - timedelta(days=5),
            return_date=today, status='RETURNED', fine_amount=Decimal('100.00'),
        )

    def test_member_sees_own_fines_and_receipts(self):
        FinePayment.objects.create(
            receipt_number='REC-T-1', transaction=self.txn, member=self.member,
            amount_paid=Decimal('40.00'), collected_by=self.staff,
        )
        c = Client(); c.force_login(self.user)
        r = c.get('/my-fines/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_outstanding'], Decimal('60.00'))
        self.assertEqual(r.context['total_paid'], Decimal('40.00'))
        self.assertContains(r, 'REC-T-1')
        self.assertEqual(c.get('/receipts/REC-T-1/').status_code, 200)

    def test_member_without_fines_gets_empty_page(self):
        BorrowTransaction.objects.all().delete()
        c = Client(); c.force_login(self.user)
        r = c.get('/my-fines/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'You have no fines')

    def test_staff_are_sent_to_full_fines_page(self):
        c = Client(); c.force_login(self.staff)
        self.assertRedirects(c.get('/my-fines/'), '/fines/')


from django.conf import settings as dj_settings

from .models import BorrowRequest, Reservation
from .services import (
    HoldError, expire_stale_holds, submit_borrow_request, approve_request, confirm_pickup,
    place_reservation, weekly_allowance,
)


class BorrowRequestWorkflowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('lib3', password='pw')
        UserProfile.objects.create(user=self.staff, role='LIBRARIAN')
        self.cat = Category.objects.create(name='Misc')
        self.book = Book.objects.create(title='One Copy', author='A', isbn='900', category=self.cat,
                                        shelf_number='S9', total_copies=1, available_copies=1)
        self.u1, self.m1 = self.make_member('a', 1)
        self.u2, self.m2 = self.make_member('b', 2)
        self.u3, self.m3 = self.make_member('c', 3)
        self.staff_client = Client(); self.staff_client.force_login(self.staff)

    def make_member(self, name, n):
        user = User.objects.create_user(name, password='pw')
        UserProfile.objects.create(user=user, role='MEMBER')
        member = Member.objects.create(user=user, member_id=f'MEM-2026-90{n}', first_name=name.upper(),
                                       last_name='Test', email=f'{name}@example.com', phone='0900')
        return user, member

    def test_request_does_not_touch_stock_and_notifies_librarian(self):
        c = Client(); c.force_login(self.u1)
        c.post(f'/books/{self.book.id}/request-borrow/')
        req = BorrowRequest.objects.get()
        self.assertEqual(req.status, 'PENDING')
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)
        self.assertTrue(Notification.objects.filter(recipient=self.staff, notification_type='LIBRARIAN_ALERT').exists())

    def test_reservation_only_when_unavailable(self):
        with self.assertRaises(HoldError):
            place_reservation(self.m1, self.book)  # a copy is free -> request to borrow instead

    def test_reservation_queue_is_first_come_first_served_and_passes_on_when_unclaimed(self):
        self.client.force_login(self.staff)
        self.client.post('/transactions/borrow/', {'member_id': self.m1.id, 'book_id': self.book.id})
        r2 = place_reservation(self.m2, self.book)
        r3 = place_reservation(self.m3, self.book)
        txn = BorrowTransaction.objects.get()
        self.client.post(f'/transactions/{txn.id}/return/')           # copy comes back
        r2.refresh_from_db(); r3.refresh_from_db(); self.book.refresh_from_db()
        self.assertEqual((r2.status, r3.status), ('READY', 'PENDING'))  # earliest first
        self.assertEqual(self.book.available_copies, 0)
        self.assertTrue(23 <= (r2.pickup_deadline - r2.notified_at).total_seconds() / 3600 <= 25)
        Reservation.objects.filter(pk=r2.pk).update(pickup_deadline=timezone.now() - timedelta(minutes=1))
        expire_stale_holds()                                           # r2 didn't collect
        r2.refresh_from_db(); r3.refresh_from_db()
        self.assertEqual((r2.status, r3.status), ('EXPIRED', 'READY'))  # next in line gets it

    def test_member_cannot_approve_requests(self):
        req = submit_borrow_request(self.m1, self.book)
        c = Client(); c.force_login(self.u1)
        self.assertEqual(c.post(f'/requests/{req.id}/approve/').status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')


class MembershipTypeTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('lib4', password='pw')
        UserProfile.objects.create(user=self.staff, role='ADMIN')  # reports are admin-only
        self.client = Client()
        self.client.force_login(self.staff)

    def create(self, **extra):
        data = {'first_name': 'Mia', 'last_name': 'Santos', 'email': 'mia@example.com', 'phone': '0917'}
        data.update(extra)
        return self.client.post('/members/new/', data)

    def test_membership_type_saved_when_adding_member(self):
        self.create(membership_type='FACULTY')
        self.assertEqual(Member.objects.get().membership_type, 'FACULTY')

    def test_missing_or_invalid_type_defaults_to_student(self):
        self.create()
        self.create(email='x@example.com', membership_type='NOPE')
        self.assertEqual(set(Member.objects.values_list('membership_type', flat=True)), {'STUDENT'})

    def test_membership_type_can_be_edited_and_is_shown(self):
        self.create()
        m = Member.objects.get()
        self.client.post(f'/members/{m.id}/edit/', {
            'first_name': 'Mia', 'last_name': 'Santos', 'email': 'mia@example.com',
            'phone': '0917', 'status': 'ACTIVE', 'membership_type': 'STAFF',
        })
        m.refresh_from_db()
        self.assertEqual(m.membership_type, 'STAFF')
        self.assertContains(self.client.get('/members/'), 'Staff')
        self.assertContains(self.client.get(f'/members/{m.id}/'), 'selected')  # edit form preselects current type

    def test_member_report_exports_include_type(self):
        self.create(membership_type='COMMUNITY')
        for fmt in ('excel', 'pdf'):
            r = self.client.get(f'/reports/export/?type=members_status&format={fmt}')
            self.assertEqual(r.status_code, 200, fmt)


# =====================================================================
# Admin role: permissions, settings, categories, librarian accounts, reports
# =====================================================================
from .models import AuditLog, BorrowingTier, LibrarySettings
from .reports import REPORT_TYPES


class AdminFeatureBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('boss', password='Str0ng-pass-1')
        UserProfile.objects.create(user=self.admin, role='ADMIN')
        self.lib = User.objects.create_user('lib', password='Str0ng-pass-2')
        UserProfile.objects.create(user=self.lib, role='LIBRARIAN')
        self.mem_user = User.objects.create_user('stud', password='Str0ng-pass-3')
        UserProfile.objects.create(user=self.mem_user, role='MEMBER')
        self.member = Member.objects.create(user=self.mem_user, member_id='MEM-2026-7001', first_name='Stu',
                                            last_name='Dent', email='s@example.com', phone='0900')
        self.cat = Category.objects.create(name='General')
        self.book = Book.objects.create(title='Book A', author='Au', isbn='7001', category=self.cat,
                                        shelf_number='A1', total_copies=3, available_copies=3)
        self.ca = self.login(self.admin)
        self.cl = self.login(self.lib)
        self.cm = self.login(self.mem_user)

    def login(self, user):
        c = Client(); c.force_login(user); return c


class RoleAccessTests(AdminFeatureBase):
    ADMIN_ONLY = ['/management/settings/', '/management/librarians/', '/reports/', '/reports/export/?type=overview',
                  '/management/users/']
    STAFF = ['/books/', '/members/', '/transactions/', '/requests/', '/reservations/', '/fines/']

    def test_admin_has_every_librarian_page(self):
        for url in self.STAFF:
            self.assertEqual(self.ca.get(url).status_code, 200, f'admin {url}')
            self.assertEqual(self.cl.get(url).status_code, 200, f'librarian {url}')
            member_ok = url in ('/books/', '/reservations/')  # members may browse the catalog and see their holds
            self.assertEqual(self.cm.get(url).status_code, 200 if member_ok else 403, f'member {url}')

    def test_admin_can_do_librarian_work(self):
        self.ca.post('/books/new/', {'title': 'New', 'author': 'X', 'isbn': '999', 'category': self.cat.id,
                                     'shelf_number': 'Z', 'copies': 1})
        self.assertTrue(Book.objects.filter(isbn='999').exists())
        r = self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        self.assertEqual(BorrowTransaction.objects.count(), 1)

    def test_admin_post_actions_forbidden_to_others(self):
        for url in ['/management/settings/categories/new/', '/management/librarians/new/']:
            self.assertEqual(self.cl.post(url, {'name': 'x'}).status_code, 403, url)
            self.assertEqual(self.cm.post(url, {'name': 'x'}).status_code, 403, url)


class SystemSettingsTests(AdminFeatureBase):
    def save(self, **kw):
        data = {'daily_fine_rate': '20.00', 'weekly_borrow_limit': '5', 'max_renewals': '1'}
        data.update({k: str(v) for k, v in kw.items()})
        return self.ca.post('/management/settings/', data)

    def test_invalid_values_rejected(self):
        for bad in [{'daily_fine_rate': '-5'}, {'weekly_borrow_limit': '0'}, {'weekly_borrow_limit': '-1'},
                    {'max_renewals': '-1'}, {'max_renewals': '9'}, {'daily_fine_rate': 'abc'}]:
            r = self.save(**bad)
            self.assertEqual(r.status_code, 200, bad)  # re-rendered with errors, not saved
        s = LibrarySettings.get()
        self.assertEqual((s.daily_fine_rate, s.weekly_borrow_limit, s.max_renewals), (Decimal('20.00'), 5, 1))

    def test_valid_update_is_saved_and_audited(self):
        self.save(daily_fine_rate='35.50', weekly_borrow_limit=3, max_renewals=2)
        s = LibrarySettings.get()
        self.assertEqual((s.daily_fine_rate, s.weekly_borrow_limit, s.max_renewals), (Decimal('35.50'), 3, 2))
        log = AuditLog.objects.filter(action='UPDATE_SETTINGS').get()
        self.assertEqual(log.user, self.admin)
        self.assertIn('20.00 -> 35.50', log.details)

    def test_borrow_duration_applies_to_new_loans_only(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        old = BorrowTransaction.objects.get()
        self.assertEqual(old.due_date - old.issue_date, timedelta(days=14))
        self.ca.post(f'/management/settings/tiers/{BorrowingTier.get_default().id}/edit/', {'name': 'Standard', 'days': 10})
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        new = BorrowTransaction.objects.exclude(pk=old.pk).get()
        old.refresh_from_db()
        self.assertEqual(new.due_date - new.issue_date, timedelta(days=10))
        self.assertEqual(old.due_date - old.issue_date, timedelta(days=14))  # untouched

    def test_fine_rate_is_not_retroactive(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        old = BorrowTransaction.objects.get()
        self.assertEqual(old.daily_fine_rate, Decimal('20.00'))
        self.save(daily_fine_rate='50.00')
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        new = BorrowTransaction.objects.exclude(pk=old.pk).get()
        self.assertEqual(new.daily_fine_rate, Decimal('50.00'))
        today = timezone.localdate()
        for t in (old, new):
            t.due_date = today - timedelta(days=2); t.issue_date = today - timedelta(days=10); t.save()
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual(old.compute_fine(), Decimal('40.00'))   # 2 days x old rate 20
        self.assertEqual(new.compute_fine(), Decimal('100.00'))  # 2 days x new rate 50
        self.ca.get(f'/transactions/{new.id}/return/')
        new.refresh_from_db()
        self.assertEqual(new.fine_amount, Decimal('100.00'))

    def test_weekly_limit_comes_from_settings(self):
        self.save(weekly_borrow_limit=2)
        for _ in range(3):
            self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        self.assertEqual(BorrowTransaction.objects.count(), 2)  # third blocked
        self.save(weekly_borrow_limit=3)
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        self.assertEqual(BorrowTransaction.objects.count(), 3)

    def test_rules_shown_in_pages_instead_of_hardcoded(self):
        self.save(daily_fine_rate='42.00')
        self.ca.post(f'/management/settings/tiers/{BorrowingTier.get_default().id}/edit/', {'name': 'Standard', 'days': 21})
        page = self.ca.get('/transactions/').content.decode()
        self.assertIn('42.00', page)
        self.assertIn('data-days="21"', page)


class CategoryTests(AdminFeatureBase):
    def test_add_edit_delete_and_protection(self):
        self.ca.post('/management/settings/categories/new/', {'name': 'Science', 'description': 'STEM'})
        sci = Category.objects.get(name='Science')
        self.ca.post('/management/settings/categories/new/', {'name': 'science'})  # duplicate (case-insensitive)
        self.assertEqual(Category.objects.filter(name__iexact='science').count(), 1)
        self.ca.post(f'/management/settings/categories/{sci.id}/edit/', {'name': 'Sciences', 'description': ''})
        sci.refresh_from_db()
        self.assertEqual(sci.name, 'Sciences')
        self.ca.post(f'/management/settings/categories/{self.cat.id}/delete/')   # has a book
        self.assertTrue(Category.objects.filter(id=self.cat.id).exists())
        self.ca.post(f'/management/settings/categories/{sci.id}/delete/')        # empty
        self.assertFalse(Category.objects.filter(id=sci.id).exists())
        actions = set(AuditLog.objects.values_list('action', flat=True))
        self.assertTrue({'CREATE_CATEGORY', 'UPDATE_CATEGORY', 'DELETE_CATEGORY'} <= actions)


class LibrarianManagementTests(AdminFeatureBase):
    def create(self, **kw):
        data = {'username': 'newlib', 'password': 'Tr1cky-Pass-77', 'first_name': 'New', 'last_name': 'Lib',
                'email': 'new@example.com', 'phone': '0911'}
        data.update(kw)
        return self.ca.post('/management/librarians/new/', data)

    def test_create_and_login_as_librarian(self):
        self.create()
        u = User.objects.get(username='newlib')
        self.assertEqual(u.profile.role, 'LIBRARIAN')
        self.assertTrue(Client().login(username='newlib', password='Tr1cky-Pass-77'))
        self.assertEqual(self.login(u).get('/management/librarians/').status_code, 403)

    def test_weak_or_duplicate_rejected(self):
        self.create(password='12345678')
        self.assertFalse(User.objects.filter(username='newlib').exists())
        self.create(username='LIB')  # duplicate of existing 'lib', case-insensitive
        self.assertEqual(User.objects.filter(username__iexact='lib').count(), 1)

    def test_edit_cannot_change_role(self):
        self.ca.post(f'/management/librarians/{self.lib.id}/edit/', {'first_name': 'Edited', 'last_name': 'Name',
                                                                      'email': 'e@example.com', 'phone': '0922', 'role': 'ADMIN'})
        self.lib.refresh_from_db()
        self.assertEqual((self.lib.first_name, self.lib.profile.role, self.lib.profile.phone), ('Edited', 'LIBRARIAN', '0922'))

    def test_deactivate_blocks_login_then_reactivate(self):
        self.ca.post(f'/management/librarians/{self.lib.id}/toggle/')
        self.assertFalse(Client().login(username='lib', password='Str0ng-pass-2'))
        self.assertEqual(self.cl.get('/books/').status_code, 302)  # existing session is rejected too
        self.ca.post(f'/management/librarians/{self.lib.id}/toggle/')
        self.assertTrue(Client().login(username='lib', password='Str0ng-pass-2'))

    def test_reset_password_custom_and_generated(self):
        self.ca.post(f'/management/librarians/{self.lib.id}/reset-password/', {'password': 'Brand-New-Pass-5'})
        self.assertTrue(Client().login(username='lib', password='Brand-New-Pass-5'))
        r = self.ca.post(f'/management/librarians/{self.lib.id}/reset-password/', {'password': ''}, follow=True)
        self.assertContains(r, 'temporary password')
        self.assertFalse(Client().login(username='lib', password='Brand-New-Pass-5'))
        log = AuditLog.objects.filter(action='RESET_LIBRARIAN_PASSWORD').first()
        self.assertNotIn('Brand-New', log.details)

    def test_admin_accounts_and_members_cannot_be_managed_here(self):
        for target in (self.admin, self.mem_user):
            self.assertEqual(self.ca.post(f'/management/librarians/{target.id}/toggle/').status_code, 404)
            self.assertEqual(self.ca.post(f'/management/librarians/{target.id}/reset-password/', {'password': 'x'}).status_code, 404)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_actions_are_audited(self):
        self.create()
        u = User.objects.get(username='newlib')
        self.ca.post(f'/management/librarians/{u.id}/toggle/')
        actions = set(AuditLog.objects.values_list('action', flat=True))
        self.assertTrue({'CREATE_LIBRARIAN', 'DEACTIVATE_LIBRARIAN'} <= actions)


class ReportTests(AdminFeatureBase):
    def test_overview_reflects_real_records(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        r = self.ca.get('/reports/')
        s = r.context['stats']
        self.assertEqual((s['total_titles'], s['total_copies'], s['total_members'], s['active_librarians'],
                          s['currently_borrowed']), (1, 3, 1, 1, 1))

    def test_date_range_filters_activity(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        today = timezone.localdate()
        inside = self.ca.get(f'/reports/?start={today}&end={today}')
        outside = self.ca.get(f'/reports/?start={today - timedelta(days=60)}&end={today - timedelta(days=30)}')
        self.assertEqual(len(inside.context['top_books']), 1)
        self.assertEqual(len(outside.context['top_books']), 0)
        self.assertEqual(sum(c['borrows'] for c in outside.context['category_rows']), 0)

    def test_reversed_dates_are_swapped(self):
        r = self.ca.get('/reports/?start=2026-12-31&end=2026-01-01')
        self.assertEqual(r.status_code, 200)
        self.assertLess(r.context['start'], r.context['end'])

    def test_every_report_exports_to_pdf_and_excel(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        for key, *_ in REPORT_TYPES:
            x = self.ca.get(f'/reports/export/?type={key}&format=excel')
            p = self.ca.get(f'/reports/export/?type={key}&format=pdf')
            self.assertEqual((x.status_code, p.status_code), (200, 200), key)
            self.assertTrue(x.content[:2] == b'PK' and p.content[:4] == b'%PDF', key)
            self.assertEqual(self.cl.get(f'/reports/export/?type={key}&format=pdf').status_code, 403, key)
        self.assertTrue(AuditLog.objects.filter(action='EXPORT_REPORT').exists())

    def test_fines_report_uses_actual_payments(self):
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.book.id})
        txn = BorrowTransaction.objects.get()
        txn.fine_amount = Decimal('100.00'); txn.save()
        self.ca.post(f'/transactions/{txn.id}/pay-fine/', {'amount': '30.00', 'payment_method': 'CASH'})
        s = self.ca.get('/reports/').context['stats']
        self.assertEqual((s['fines_collected'], s['fines_outstanding']), (Decimal('30.00'), Decimal('70.00')))


# =====================================================================
# Borrowing tiers, demand suggestions and renewals
# =====================================================================
from .demand import demand_for_books, suggest_tier
from .services import place_reservation, renew_loan, renewal_block_reason


class BorrowingTierTests(AdminFeatureBase):
    def tier(self, name):
        return BorrowingTier.objects.get(name=name)

    def borrow(self, book=None, member=None):
        self.ca.post('/transactions/borrow/', {'member_id': (member or self.member).id, 'book_id': (book or self.book).id})
        return BorrowTransaction.objects.latest('id')

    # ---- defaults and resolution order
    def test_default_tiers_exist(self):
        self.assertEqual(
            list(BorrowingTier.objects.values_list('name', 'days', 'is_default').order_by('days')),
            [('Short-term', 7, False), ('Standard', 14, True), ('Extended', 30, False)],
        )

    def test_book_tier_beats_category_tier_beats_default(self):
        self.assertEqual(self.book.resolve_loan_terms().days, 14)
        self.assertEqual(self.book.resolve_loan_terms().source, 'default')
        self.cat.borrowing_tier = self.tier('Extended'); self.cat.save()
        self.book.refresh_from_db()
        t = self.book.resolve_loan_terms()
        self.assertEqual((t.days, t.source), (30, 'category'))
        self.book.borrowing_tier = self.tier('Short-term'); self.book.save()
        t = self.book.resolve_loan_terms()
        self.assertEqual((t.days, t.source), (7, 'book'))

    def test_inactive_tier_is_skipped(self):
        self.book.borrowing_tier = self.tier('Short-term'); self.book.save()
        self.ca.post(f"/management/settings/tiers/{self.tier('Short-term').id}/toggle/")
        self.book.refresh_from_db()
        self.assertEqual(self.book.resolve_loan_terms().days, 14)

    # ---- due dates

    def test_editing_a_tier_never_changes_existing_loans(self):
        old = self.borrow()
        self.ca.post(f"/management/settings/tiers/{self.tier('Standard').id}/edit/", {'name': 'Standard', 'days': 3})
        old.refresh_from_db()
        self.assertEqual((old.due_date - old.issue_date).days, 14)
        self.assertEqual(old.loan_days, 14)
        self.assertEqual((self.borrow().due_date - timezone.localdate()).days, 3)

    # ---- admin management
    def test_admin_can_add_edit_deactivate_and_set_default(self):
        self.ca.post('/management/settings/tiers/new/', {'name': 'Overnight', 'days': 1})
        t = self.tier('Overnight')
        self.assertEqual(t.days, 1)
        self.ca.post(f'/management/settings/tiers/{t.id}/edit/', {'name': 'One day', 'days': 2})
        t.refresh_from_db()
        self.assertEqual((t.name, t.days), ('One day', 2))
        self.ca.post(f'/management/settings/tiers/{t.id}/toggle/')
        t.refresh_from_db()
        self.assertFalse(t.is_active)
        self.ca.post(f'/management/settings/tiers/{t.id}/make-default/')  # inactive: refused
        t.refresh_from_db()
        self.assertFalse(t.is_default)
        self.ca.post(f"/management/settings/tiers/{self.tier('Extended').id}/make-default/")
        self.assertEqual(BorrowingTier.get_default().name, 'Extended')
        self.assertEqual(BorrowingTier.objects.filter(is_default=True).count(), 1)
        self.assertTrue(AuditLog.objects.filter(action='CREATE_TIER').exists())

    def test_invalid_tiers_rejected(self):
        before = BorrowingTier.objects.count()
        for bad in [{'name': '', 'days': 5}, {'name': 'X', 'days': 0}, {'name': 'X', 'days': 366},
                    {'name': 'X', 'days': 'abc'}, {'name': 'standard', 'days': 5}]:
            self.ca.post('/management/settings/tiers/new/', bad)
        self.assertEqual(BorrowingTier.objects.count(), before)

    def test_default_tier_cannot_be_deactivated(self):
        std = self.tier('Standard')
        self.ca.post(f'/management/settings/tiers/{std.id}/toggle/')
        std.refresh_from_db()
        self.assertTrue(std.is_active)

    # ---- assignment
    def test_category_tier_assignment(self):
        ext = self.tier('Extended')
        self.ca.post('/management/settings/categories/new/', {'name': 'Reference', 'borrowing_tier': ext.id})
        self.assertEqual(Category.objects.get(name='Reference').borrowing_tier, ext)
        self.ca.post(f'/management/settings/categories/{self.cat.id}/edit/',
                     {'name': 'General', 'borrowing_tier': self.tier('Short-term').id})
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.borrowing_tier.name, 'Short-term')
        self.ca.post(f'/management/settings/categories/{self.cat.id}/edit/', {'name': 'General', 'borrowing_tier': ''})
        self.cat.refresh_from_db()
        self.assertIsNone(self.cat.borrowing_tier)

    def edit_book(self, client, **extra):
        data = {'title': 'Book A', 'author': 'Au', 'isbn': '7001', 'category': self.cat.id, 'shelf_number': 'A1',
                'status': 'AVAILABLE', 'description': ''}
        data.update(extra)
        return client.post(f'/books/{self.book.id}/edit/', data)

    def test_admin_assigns_book_tier_but_librarian_cannot(self):
        self.edit_book(self.cl, borrowing_tier=self.tier('Short-term').id)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.borrowing_tier)
        self.edit_book(self.ca, borrowing_tier=self.tier('Short-term').id)
        self.book.refresh_from_db()
        self.assertEqual(self.book.borrowing_tier.name, 'Short-term')
        self.edit_book(self.cl)  # a librarian saving other changes leaves the tier alone
        self.book.refresh_from_db()
        self.assertEqual(self.book.borrowing_tier.name, 'Short-term')
        self.edit_book(self.ca, borrowing_tier='')
        self.book.refresh_from_db()
        self.assertIsNone(self.book.borrowing_tier)

    def test_saving_a_book_keeps_its_deactivated_tier(self):
        self.book.borrowing_tier = self.tier('Extended'); self.book.save()
        self.ca.post(f"/management/settings/tiers/{self.tier('Extended').id}/toggle/")
        self.edit_book(self.ca, borrowing_tier=self.tier('Extended').id)
        self.book.refresh_from_db()
        self.assertEqual(self.book.borrowing_tier.name, 'Extended')


class RenewalTests(AdminFeatureBase):
    def setUp(self):
        super().setUp()
        self.txn = self.borrow()

    def borrow(self, book=None, member=None):
        self.ca.post('/transactions/borrow/', {'member_id': (member or self.member).id, 'book_id': (book or self.book).id})
        return BorrowTransaction.objects.latest('id')

    def other_member(self):
        u = User.objects.create_user('other', password='Str0ng-pass-4')
        UserProfile.objects.create(user=u, role='MEMBER')
        return Member.objects.create(user=u, member_id='MEM-2026-7002', first_name='Oth', last_name='Er',
                                     email='o@example.com', phone='0901')

    def test_member_renews_once_for_the_same_duration(self):
        due = self.txn.due_date
        self.cm.post(f'/transactions/{self.txn.id}/renew/')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.due_date, due + timedelta(days=14))
        self.assertEqual(self.txn.renewal_count, 1)
        self.cm.post(f'/transactions/{self.txn.id}/renew/')  # second renewal is refused
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.due_date, due + timedelta(days=14))
        self.assertTrue(Notification.objects.filter(recipient=self.mem_user, title__startswith='Loan Renewed').exists())

    def test_renewal_uses_the_loans_own_length_even_if_tier_changed(self):
        due = self.txn.due_date
        std = BorrowingTier.objects.get(name='Standard'); std.days = 3; std.save()
        renew_loan(self.txn.id)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.due_date, due + timedelta(days=14))

    def test_admin_can_allow_more_renewals_or_turn_them_off(self):
        s = LibrarySettings.get(); s.max_renewals = 2; s.save()
        renew_loan(self.txn.id); renew_loan(self.txn.id)
        with self.assertRaises(HoldError):
            renew_loan(self.txn.id)
        s.max_renewals = 0; s.save()
        self.assertIn('turned off', renewal_block_reason(BorrowTransaction.objects.get(pk=self.txn.pk)))

    def test_overdue_loan_cannot_be_renewed(self):
        today = timezone.localdate()
        BorrowTransaction.objects.filter(pk=self.txn.pk).update(issue_date=today - timedelta(days=20), due_date=today - timedelta(days=1), status='OVERDUE')
        with self.assertRaises(HoldError):
            renew_loan(self.txn.id)

    def test_reserved_book_cannot_be_renewed(self):
        single = Book.objects.create(title='One copy', author='A', isbn='7777', category=self.cat,
                                     shelf_number='B', total_copies=1, available_copies=1)
        txn = self.borrow(book=single)
        place_reservation(self.other_member(), single)
        with self.assertRaises(HoldError) as ctx:
            renew_loan(txn.id)
        self.assertIn('reserved', str(ctx.exception))

    def test_member_with_unpaid_fines_or_inactive_cannot_renew(self):
        self.member.balance = Decimal('20.00'); self.member.save()
        with self.assertRaises(HoldError):
            renew_loan(self.txn.id)
        self.member.balance = Decimal('0.00'); self.member.status = 'SUSPENDED'; self.member.save()
        with self.assertRaises(HoldError):
            renew_loan(self.txn.id)

    def test_members_cannot_renew_other_peoples_loans_and_anonymous_is_redirected(self):
        other = self.other_member()
        c = self.login(other.user)
        c.post(f'/transactions/{self.txn.id}/renew/')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.renewal_count, 0)
        self.assertEqual(Client().post(f'/transactions/{self.txn.id}/renew/').status_code, 302)
        self.assertEqual(self.cm.get(f'/transactions/{self.txn.id}/renew/').status_code, 405)

    def test_staff_can_renew_on_behalf_of_member(self):
        self.cl.post(f'/transactions/{self.txn.id}/renew/')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.renewal_count, 1)
        self.assertTrue(AuditLog.objects.filter(action='RENEW_LOAN').exists())

    def test_member_dashboard_shows_renew_button_then_reason(self):
        self.assertIn(f'/transactions/{self.txn.id}/renew/', self.cm.get('/').content.decode())
        renew_loan(self.txn.id)
        self.assertIn('Already renewed once', self.cm.get('/').content.decode())


class DemandAndSuggestionTests(AdminFeatureBase):
    def setUp(self):
        super().setUp()
        self.one = Book.objects.create(title='Popular', author='A', isbn='8001', category=self.cat,
                                       shelf_number='P', total_copies=1, available_copies=1)

    def age(self, book, days=100):
        Book.objects.filter(pk=book.pk).update(created_at=timezone.now() - timedelta(days=days))
        book.refresh_from_db()
        return book

    def demand(self, book):
        return demand_for_books([book])[book.id]

    def test_new_book_with_no_history_is_low_demand_but_flagged_new(self):
        d = self.demand(self.one)
        self.assertEqual((d.level, d.is_new), ('LOW', True))
        self.assertEqual(suggest_tier(d).name, 'Standard')  # newly acquired -> default duration

    def test_old_unborrowed_book_is_low_demand_and_suggests_longest_tier(self):
        d = self.demand(self.age(self.one))
        self.assertEqual((d.level, d.is_new), ('LOW', False))
        self.assertEqual(suggest_tier(d).name, 'Extended')

    def test_reservation_queue_makes_a_book_high_demand(self):
        self.age(self.one)
        m2 = Member.objects.create(member_id='MEM-2026-8002', first_name='B', last_name='C', email='b@example.com', phone='1')
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.one.id})
        place_reservation(m2, self.one)
        m3 = Member.objects.create(member_id='MEM-2026-8004', first_name='D', last_name='E', email='d@example.com', phone='1')
        place_reservation(m3, self.one)
        d = self.demand(self.one)
        self.assertEqual(d.level, 'HIGH')
        self.assertEqual(suggest_tier(d).name, 'Short-term')

    def test_many_recent_loans_per_copy_is_high_demand(self):
        self.age(self.one)
        today = timezone.localdate()
        for i in range(4):
            BorrowTransaction.objects.create(
                transaction_code=f'TXN-D-{i}', member=self.member, book=self.one, issued_by=self.admin,
                issue_date=today - timedelta(days=10 + i), due_date=today - timedelta(days=i), status='RETURNED',
                return_date=today - timedelta(days=i))
        self.assertEqual(self.demand(self.one).level, 'HIGH')

    def test_old_loans_outside_the_window_do_not_count(self):
        self.age(self.one)
        today = timezone.localdate()
        for i in range(5):
            BorrowTransaction.objects.create(
                transaction_code=f'TXN-O-{i}', member=self.member, book=self.one, issued_by=self.admin,
                issue_date=today - timedelta(days=200), due_date=today - timedelta(days=186), status='RETURNED',
                return_date=today - timedelta(days=190))
        self.assertEqual(self.demand(self.one).level, 'LOW')

    def test_demand_lookup_uses_a_fixed_number_of_queries(self):
        books = [self.age(Book.objects.create(title=f'B{i}', author='A', isbn=f'90{i}', category=self.cat,
                                              shelf_number='X', total_copies=1, available_copies=1)) for i in range(8)]
        with self.assertNumQueries(3):
            demand_for_books(books)

    def test_suggested_duration_appears_on_librarian_borrow_page_not_admin_settings(self):
        self.age(self.one)
        borrow_page = self.ca.get('/transactions/').content.decode()
        settings_page = self.ca.get('/management/settings/').content.decode()
        self.assertIn('data-days="30"', borrow_page)
        self.assertIn('data-demand="Low demand"', borrow_page)
        self.assertNotIn('Suggested Tier Changes', settings_page)

    def test_checkout_uses_the_suggested_duration(self):
        self.age(self.one)
        response = self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.one.id})
        self.assertEqual(response.status_code, 302)
        txn = BorrowTransaction.objects.filter(book=self.one).latest('id')
        self.assertEqual(txn.loan_days, 30)
        self.assertEqual(txn.tier_name, 'Extended')

    def test_high_demand_badge_on_catalog(self):
        self.age(self.one)
        self.ca.post('/transactions/borrow/', {'member_id': self.member.id, 'book_id': self.one.id})
        m2 = Member.objects.create(member_id='MEM-2026-8003', first_name='B', last_name='C', email='c@example.com', phone='1')
        place_reservation(m2, self.one)
        m3 = Member.objects.create(member_id='MEM-2026-8005', first_name='D', last_name='E', email='d2@example.com', phone='1')
        place_reservation(m3, self.one)
        self.assertIn('High demand', self.cm.get('/books/').content.decode())



class BookReviewTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name='Fiction')
        self.book = Book.objects.create(
            title='Dune', author='Frank Herbert', isbn='222', category=cat,
            shelf_number='S2', total_copies=1, available_copies=1,
        )
        self.users, self.members = {}, {}
        for i, name in enumerate(['ana', 'ben']):
            u = User.objects.create_user(name, password='pw')
            UserProfile.objects.create(user=u, role='MEMBER')
            m = Member.objects.create(
                user=u, member_id=f'MEM-2026-000{i + 1}', first_name=name.title(), last_name='Cruz',
                email=f'{name}@example.com', phone='0917',
            )
            self.users[name], self.members[name] = u, m
        self.lib = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=self.lib, role='LIBRARIAN')
        self.url = f'/books/{self.book.id}/review/'

    def login(self, name):
        c = Client()
        c.force_login(self.users[name] if name in self.users else self.lib)
        return c

    def test_member_can_post_and_everyone_sees_it(self):
        self.login('ana').post(self.url, {'rating': 5, 'comment': 'Brilliant sci-fi classic.'})
        review = BookReview.objects.get()
        self.assertEqual((review.rating, review.member, review.book), (5, self.members['ana'], self.book))
        page = self.login('ben').get(f'/books/{self.book.id}/')
        self.assertContains(page, 'Brilliant sci-fi classic.')
        self.assertEqual(self.book.review_summary, {'average': 5.0, 'count': 1})

    def test_profanity_is_rejected_including_evasions(self):
        for text in ['this is shit', 'what a f*ck1ng mess', 'F U C K', 'fuuuuck this', 'Putang ina mo', 'gago ang author']:
            resp = self.login('ana').post(self.url, {'rating': 1, 'comment': text})
            self.assertContains(resp, 'language that is not allowed', msg_prefix=text)
        self.assertEqual(BookReview.objects.count(), 0)
        self.assertEqual(AuditLog.objects.filter(action='REVIEW_BLOCKED').count(), 6)
        self.assertFalse(AuditLog.objects.filter(details__icontains='shit').exists())  # offending text is not logged

    def test_innocent_words_are_not_flagged(self):
        text = 'A classic. The assassin scene, the Scunthorpe anecdote and the passage on grass were great.'
        self.login('ana').post(self.url, {'rating': 4, 'comment': text})
        self.assertEqual(BookReview.objects.get().comment, text)

    def test_links_and_contact_details_are_rejected(self):
        for text in ['visit www.spam.com', 'email me at a@b.com', 'text 09171234567']:
            self.login('ana').post(self.url, {'rating': 3, 'comment': text})
        self.assertEqual(BookReview.objects.count(), 0)

    def test_rating_must_be_1_to_5(self):
        for bad in ['0', '6', '', 'abc']:
            self.login('ana').post(self.url, {'rating': bad, 'comment': 'ok'})
        self.assertEqual(BookReview.objects.count(), 0)

    def test_one_review_per_member_resubmit_edits(self):
        c = self.login('ana')
        c.post(self.url, {'rating': 2, 'comment': 'meh'})
        c.post(self.url, {'rating': 4, 'comment': 'grew on me'})
        review = BookReview.objects.get()
        self.assertEqual((review.rating, review.comment), (4, 'grew on me'))

    def test_comment_is_optional(self):
        self.login('ana').post(self.url, {'rating': 3, 'comment': ''})
        self.assertEqual(BookReview.objects.get().rating, 3)

    def test_html_in_comment_is_escaped(self):
        self.login('ana').post(self.url, {'rating': 3, 'comment': '<script>alert(1)</script>'})
        page = self.login('ben').get(f'/books/{self.book.id}/')
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_staff_cannot_post_and_anonymous_is_redirected(self):
        self.login('lib').post(self.url, {'rating': 5, 'comment': 'hi'})
        self.assertEqual(BookReview.objects.count(), 0)
        resp = Client().post(self.url, {'rating': 5})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_suspended_member_cannot_post(self):
        Member.objects.filter(pk=self.members['ana'].pk).update(status='SUSPENDED')
        self.login('ana').post(self.url, {'rating': 5, 'comment': 'hi'})
        self.assertEqual(BookReview.objects.count(), 0)

    def test_delete_permissions(self):
        review = BookReview.objects.create(book=self.book, member=self.members['ana'], rating=4, comment='x')
        url = f'/reviews/{review.id}/delete/'
        self.assertEqual(self.login('ben').post(url).status_code, 403)
        self.assertTrue(BookReview.objects.exists())
        self.login('ana').post(url)
        self.assertFalse(BookReview.objects.exists())

    def test_librarian_can_delete_and_hide(self):
        review = BookReview.objects.create(book=self.book, member=self.members['ana'], rating=4, comment='visible text')
        self.login('lib').post(f'/reviews/{review.id}/toggle-hidden/')
        review.refresh_from_db()
        self.assertTrue(review.is_hidden)
        self.assertNotContains(self.login('ben').get(f'/books/{self.book.id}/'), 'visible text')
        self.assertContains(self.login('lib').get(f'/books/{self.book.id}/'), 'visible text')
        self.assertEqual(self.book.review_summary['count'], 0)  # hidden reviews do not count
        self.login('lib').post(f'/reviews/{review.id}/delete/')
        self.assertFalse(BookReview.objects.exists())

    def test_member_cannot_hide_reviews_and_get_is_not_allowed(self):
        review = BookReview.objects.create(book=self.book, member=self.members['ana'], rating=4)
        self.assertEqual(self.login('ben').post(f'/reviews/{review.id}/toggle-hidden/').status_code, 403)
        self.assertEqual(self.login('ana').get(self.url).status_code, 405)

    def test_book_list_shows_average(self):
        BookReview.objects.create(book=self.book, member=self.members['ana'], rating=5)
        BookReview.objects.create(book=self.book, member=self.members['ben'], rating=4)
        self.assertContains(self.login('ana').get('/books/'), '4.5 (2)')


class BookReviewBlockedReasonTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name='Fiction')
        self.book = Book.objects.create(title='T', author='A', isbn='9', category=cat, shelf_number='S', total_copies=1, available_copies=1)

    def test_unlinked_member_account_sees_why(self):
        u = User.objects.create_user('g', password='pw')
        UserProfile.objects.create(user=u, role='MEMBER')
        c = Client(); c.force_login(u)
        self.assertContains(c.get(f'/books/{self.book.id}/'), "isn&#x27;t linked to a library membership")

    def test_inactive_member_sees_why(self):
        u = User.objects.create_user('h', password='pw')
        UserProfile.objects.create(user=u, role='MEMBER')
        Member.objects.create(user=u, member_id='M1', first_name='H', last_name='X', email='h@x.com', phone='1', status='INACTIVE')
        c = Client(); c.force_login(u)
        self.assertContains(c.get(f'/books/{self.book.id}/'), 'membership is inactive')


class OverdueSuspensionTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name='Fiction')
        self.books = [Book.objects.create(title=f'B{i}', author='A', isbn=f'90{i}', category=cat, shelf_number='S', total_copies=3, available_copies=3) for i in range(6)]
        self.user = User.objects.create_user('stu', password='pw')
        UserProfile.objects.create(user=self.user, role='MEMBER')
        self.member = Member.objects.create(user=self.user, member_id='M1', first_name='S', last_name='T', email='s@x.com', phone='1')
        self.lib = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=self.lib, role='LIBRARIAN')

    def loan(self, book=None, days_ago=10, due_in=-3):
        today = timezone.localdate()
        return BorrowTransaction.objects.create(
            transaction_code=BorrowTransaction.generate_transaction_code(), member=self.member, book=book or self.books[0],
            issued_by=self.lib, issue_date=today - timedelta(days=days_ago), due_date=today + timedelta(days=due_in), loan_days=7)

    def give_back(self, txn, on=None):
        txn.status, txn.return_date = 'RETURNED', on or timezone.localdate()
        txn.save()
        from .services import on_loan_returned
        on_loan_returned(txn)

    def sync(self):
        from .services import sync_suspensions
        sync_suspensions()
        self.member.refresh_from_db()

    def test_suspended_the_day_after_due_date_not_before(self):
        self.loan(due_in=0)  # due today: not overdue yet
        self.sync()
        self.assertEqual(self.member.status, 'ACTIVE')
        BorrowTransaction.objects.all().delete()
        self.loan(due_in=-1)  # became overdue today
        self.sync()
        self.assertEqual((self.member.status, self.member.suspension_strikes, self.member.auto_suspended), ('SUSPENDED', 1, True))
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Membership suspended').exists())
        self.assertTrue(AuditLog.objects.filter(action='AUTO_SUSPEND').exists())

    def test_suspended_member_cannot_request_and_automatic_reactivation_on_return(self):
        from .services import submit_borrow_request, HoldError
        txn = self.loan()
        self.sync()
        with self.assertRaises(HoldError):
            submit_borrow_request(self.member, self.books[1])
        self.give_back(txn)
        self.member.refresh_from_db()
        self.assertEqual((self.member.status, self.member.auto_suspended), ('ACTIVE', False))
        self.assertEqual(self.member.suspension_strikes, 1)  # late return does not clear the strike
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Membership reactivated').exists())

    def test_one_unreturned_book_only_costs_one_strike_even_after_manual_reactivation(self):
        self.loan()
        self.sync()
        self.client.force_login(self.lib)
        self.client.post(f'/members/{self.member.id}/toggle-status/')  # librarian override
        self.sync()
        self.assertEqual((self.member.status, self.member.suspension_strikes), ('ACTIVE', 1))

    def test_manual_suspension_is_never_reactivated_automatically(self):
        Member.objects.filter(pk=self.member.pk).update(status='SUSPENDED', auto_suspended=False)
        self.sync()
        self.assertEqual(self.member.status, 'SUSPENDED')

    def strike(self, n_times, return_late=True):
        for _ in range(n_times):
            txn = self.loan()
            self.sync()
            self.assertEqual(self.member.status, 'SUSPENDED')
            self.give_back(txn)
            if self.member.suspended_until:  # lock: pretend the week has passed
                Member.objects.filter(pk=self.member.pk).update(suspended_until=timezone.now() - timedelta(seconds=1))
                self.sync()
            self.member.refresh_from_db()
            self.assertEqual(self.member.status, 'ACTIVE')

    def test_third_strike_shortens_loans_to_shortest_tier(self):
        from .services import checkout_at_desk
        self.strike(2)
        txn = checkout_at_desk(self.member, self.books[2].id, self.lib, days=14)
        self.assertEqual(txn.loan_days, 14)  # still normal after 2 strikes
        self.give_back(txn, on=timezone.localdate())
        self.assertEqual(Member.objects.get(pk=self.member.pk).suspension_strikes, 0)  # on-time return reset it
        self.strike(3)
        self.assertTrue(self.member.has_shortened_loans)
        from .services import shortest_loan_days
        shortest = shortest_loan_days()
        self.assertLess(shortest, 14)
        txn = checkout_at_desk(self.member, self.books[3].id, self.lib, days=14)
        self.assertEqual((txn.loan_days, txn.tier_name), (shortest, f'Restricted ({shortest} days)'))
        self.assertEqual(txn.due_date, timezone.localdate() + timedelta(days=shortest))

    def test_fourth_strike_locks_for_a_week_even_after_return(self):
        self.strike(3)
        txn = self.loan()
        self.sync()
        self.assertEqual(self.member.suspension_strikes, 4)
        self.assertGreater(self.member.suspended_until, timezone.now() + timedelta(days=6, hours=23))
        self.give_back(txn)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, 'SUSPENDED')  # book returned, but still locked
        Member.objects.filter(pk=self.member.pk).update(suspended_until=timezone.now() - timedelta(minutes=1))
        self.sync()
        self.assertEqual(self.member.status, 'ACTIVE')

    def test_on_time_return_resets_the_streak(self):
        self.strike(2)
        txn = self.loan(days_ago=2, due_in=5)
        self.give_back(txn)  # returned before the due date
        self.member.refresh_from_db()
        self.assertEqual(self.member.suspension_strikes, 0)

    def test_profile_page_explains_suspension(self):
        self.loan()
        self.client.force_login(self.user)
        resp = self.client.get('/my-profile/')
        self.assertContains(resp, 'Suspended automatically')


class BookCopyTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name='Fiction')
        self.book = Book.objects.create(title='Dune', author='F', isbn='C1', category=cat, shelf_number='S', total_copies=3, available_copies=3)
        self.lib = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=self.lib, role='LIBRARIAN')
        self.members = []
        for i in range(3):
            u = User.objects.create_user(f'm{i}', password='pw')
            UserProfile.objects.create(user=u, role='MEMBER')
            self.members.append(Member.objects.create(user=u, member_id=f'M{i}', first_name=f'M{i}', last_name='X', email=f'm{i}@x.com', phone='1'))

    def book_now(self):
        return Book.objects.get(pk=self.book.pk)

    def copy(self, n):
        return BookCopy.objects.get(book=self.book, copy_number=n)

    def test_every_copy_is_named(self):
        codes = list(self.book.copies.values_list('copy_code', flat=True))
        self.assertEqual(codes, [f'BK{self.book.pk:04d}-C01', f'BK{self.book.pk:04d}-C02', f'BK{self.book.pk:04d}-C03'])

    def test_lost_copy_lowers_available_and_status_follows(self):
        from .services import set_copy_status
        set_copy_status(self.copy(1).id, 'LOST', self.lib, 'Not on the shelf')
        b = self.book_now()
        self.assertEqual((b.total_copies, b.available_copies, b.status), (3, 2, 'AVAILABLE'))
        self.assertEqual(b.copy_counts, {'circulating': 2, 'under_repair': 0, 'lost': 1})
        self.assertTrue(AuditLog.objects.filter(action='COPY_STATUS', details__contains='Lost').exists())

    def test_cannot_borrow_when_every_copy_is_lost_or_in_repair(self):
        from .services import set_copy_status, submit_borrow_request, checkout_at_desk, HoldError
        set_copy_status(self.copy(1).id, 'LOST', self.lib)
        set_copy_status(self.copy(2).id, 'UNDER_REPAIR', self.lib)
        set_copy_status(self.copy(3).id, 'LOST', self.lib)
        b = self.book_now()
        self.assertEqual((b.available_copies, b.status), (0, 'UNDER_REPAIR'))
        with self.assertRaises(HoldError):
            submit_borrow_request(self.members[0], b)
        with self.assertRaises(HoldError):
            checkout_at_desk(self.members[0], b.id, self.lib)

    def test_restoring_a_copy_brings_availability_back(self):
        from .services import set_copy_status
        set_copy_status(self.copy(1).id, 'UNDER_REPAIR', self.lib)
        self.assertEqual(self.book_now().available_copies, 2)
        set_copy_status(self.copy(1).id, 'IN_CIRCULATION', self.lib)
        self.assertEqual(self.book_now().available_copies, 3)

    def test_loan_uses_a_specific_copy_and_skips_bad_ones(self):
        from .services import set_copy_status, checkout_at_desk
        set_copy_status(self.copy(1).id, 'UNDER_REPAIR', self.lib)
        t1 = checkout_at_desk(self.members[0], self.book.id, self.lib)
        t2 = checkout_at_desk(self.members[1], self.book.id, self.lib)
        self.assertEqual((t1.copy.copy_number, t2.copy.copy_number), (2, 3))
        self.assertEqual(self.book_now().available_copies, 0)

    def test_damaged_copy_returned_does_not_go_back_on_shelf(self):
        from .services import set_copy_status, checkout_at_desk
        txn = checkout_at_desk(self.members[0], self.book.id, self.lib)
        self.assertEqual(self.book_now().available_copies, 2)
        set_copy_status(txn.copy_id, 'UNDER_REPAIR', self.lib, 'Torn cover')  # on loan: shelf count unchanged
        self.assertEqual(self.book_now().available_copies, 2)
        self.client.force_login(self.lib)
        self.client.post(f'/transactions/{txn.id}/return/')
        self.assertEqual(self.book_now().available_copies, 2)  # not re-shelved
        set_copy_status(txn.copy_id, 'IN_CIRCULATION', self.lib)  # repaired
        self.assertEqual(self.book_now().available_copies, 3)

    def test_good_copy_returned_goes_back_on_shelf(self):
        from .services import checkout_at_desk
        txn = checkout_at_desk(self.members[0], self.book.id, self.lib)
        self.client.force_login(self.lib)
        self.client.post(f'/transactions/{txn.id}/return/')
        self.assertEqual(self.book_now().available_copies, 3)

    def test_withdrawing_a_held_copy_bumps_the_latest_hold(self):
        from .services import set_copy_status, checkout_at_desk, place_reservation
        # Two copies loaned out, the third is on the shelf, then held for a reservation
        checkout_at_desk(self.members[0], self.book.id, self.lib)
        checkout_at_desk(self.members[1], self.book.id, self.lib)
        Book.objects.filter(pk=self.book.pk).update(available_copies=0)
        res = place_reservation(self.members[2], self.book_now())
        Book.objects.filter(pk=self.book.pk).update(available_copies=1)
        from .services import promote_reservations
        promote_reservations(self.book_now())
        res.refresh_from_db()
        self.assertEqual(res.status, 'READY')
        self.assertEqual(self.book_now().available_copies, 0)
        set_copy_status(self.copy(3).id, 'LOST', self.lib)  # the held copy is withdrawn
        res.refresh_from_db()
        self.assertEqual(res.status, 'PENDING')  # back in the queue
        self.assertEqual(self.book_now().available_copies, 0)
        self.assertTrue(Notification.objects.filter(recipient=self.members[2].user, title__startswith='Reservation update').exists())

    def test_restoring_a_copy_serves_the_waiting_reservation(self):
        from .services import set_copy_status, place_reservation
        for n in (1, 2, 3):
            set_copy_status(self.copy(n).id, 'UNDER_REPAIR', self.lib)
        # Title is Under Repair so reservations are blocked until a copy is back; reserve via ORM
        Reservation.objects.create(book=self.book, member=self.members[0], status='PENDING')
        set_copy_status(self.copy(2).id, 'IN_CIRCULATION', self.lib)
        res = Reservation.objects.get()
        self.assertEqual(res.status, 'READY')
        self.assertEqual(self.book_now().available_copies, 0)

    def test_staff_actions_and_permissions(self):
        member_client = Client(); member_client.force_login(self.members[0].user)
        resp = member_client.post(f'/copies/{self.copy(1).id}/status/', {'status': 'LOST'})
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self.copy(1).status, 'IN_CIRCULATION')
        self.client.force_login(self.lib)
        self.client.post(f'/copies/{self.copy(1).id}/status/', {'status': 'LOST', 'notes': 'gone'})
        self.assertEqual(self.copy(1).status, 'LOST')
        self.client.post(f'/books/{self.book.id}/copies/add/')
        b = self.book_now()
        self.assertEqual((b.total_copies, b.available_copies, b.copies.count()), (4, 3, 4))
        self.assertEqual(self.copy(4).copy_code, f'BK{self.book.pk:04d}-C04')

    def test_copies_table_is_staff_only(self):
        self.client.force_login(self.lib)
        self.assertContains(self.client.get(f'/books/{self.book.id}/'), 'Update condition')
        c = Client(); c.force_login(self.members[0].user)
        self.assertNotContains(c.get(f'/books/{self.book.id}/'), 'Update condition')

    def test_creating_a_book_with_copies_via_form(self):
        self.client.force_login(self.lib)
        self.client.post('/books/new/', {'title': 'New', 'author': 'A', 'isbn': 'NEW1', 'category': Category.objects.first().id,
                                         'shelf_number': 'Z', 'copies': 4})
        b = Book.objects.get(isbn='NEW1')
        self.assertEqual(b.copies.count(), 4)


class ReceiptPrintLayoutTests(TestCase):
    def test_receipt_page_has_printable_sheet_and_hides_controls_when_printing(self):
        cat = Category.objects.create(name='Fiction')
        book = Book.objects.create(title='Dune', author='F', isbn='R1', category=cat, shelf_number='S', total_copies=1, available_copies=1)
        lib = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=lib, role='LIBRARIAN')
        member = Member.objects.create(member_id='M1', first_name='Ana', last_name='Cruz', email='a@x.com', phone='1')
        txn = BorrowTransaction.objects.create(
            transaction_code='TXN-R1', member=member, book=book, issued_by=lib,
            issue_date=timezone.localdate() - timedelta(days=20), due_date=timezone.localdate() - timedelta(days=6), loan_days=14)
        FinePayment.objects.create(receipt_number='REC-R-1', transaction=txn, member=member, amount_paid='30.00', collected_by=lib)
        c = Client(); c.force_login(lib)
        page = c.get('/receipts/REC-R-1/')
        self.assertContains(page, 'class="receipt-sheet"')
        self.assertContains(page, 'Official Receipt')
        self.assertContains(page, 'Authorized signature')
        self.assertContains(page, 'no-print')  # the Back / Print / Download buttons are hidden when printing
        self.assertNotContains(page, 'Kabuuang')


class UsernameChangeTests(TestCase):
    def setUp(self):
        self.users = {}
        self.members = {}
        for i, name in enumerate(['ana', 'ben']):
            u = User.objects.create_user(name, password='pw', email=f'{name}@x.com')
            UserProfile.objects.create(user=u, role='MEMBER')
            self.users[name] = u
            self.members[name] = Member.objects.create(
                user=u, member_id=f'M{i}', first_name=name.title(), last_name='Cruz', email=f'{name}@x.com', phone='0917')
        self.lib = User.objects.create_user('lib', password='pw')
        UserProfile.objects.create(user=self.lib, role='LIBRARIAN')

    def post_profile(self, who, username, **extra):
        c = Client()
        c.force_login(self.users[who])
        data = {'username': username, 'first_name': 'Ana', 'last_name': 'Cruz', 'phone': '09171234567', 'address': '', 'date_of_birth': ''}
        data.update(extra)
        return c.post('/my-profile/', data)

    def test_member_can_change_own_username_and_stays_logged_in(self):
        resp = self.post_profile('ana', 'ana.cruz_2026')
        self.assertEqual(resp.status_code, 302)
        self.users['ana'].refresh_from_db()
        self.assertEqual(self.users['ana'].username, 'ana.cruz_2026')
        self.assertTrue(AuditLog.objects.filter(action='CHANGE_USERNAME', details__contains="'ana' to 'ana.cruz_2026'").exists())
        self.assertTrue(Client().login(username='ana.cruz_2026', password='pw'))
        self.assertFalse(Client().login(username='ana', password='pw'))

    def test_rejects_taken_case_insensitively(self):
        resp = self.post_profile('ana', 'BEN')
        self.assertContains(resp, 'already taken')
        self.users['ana'].refresh_from_db()
        self.assertEqual(self.users['ana'].username, 'ana')

    def test_rejects_bad_formats_reserved_and_profanity(self):
        for bad, text in [('ab', '3 to 30'), ('has space', '3 to 30'), ('x' * 31, 'at most 30'), ('a$b!c', '3 to 30'),
                          ('Admin', 'reserved'), ('librarian', 'reserved'), ('f_u_c_k', 'not allowed'), ('putanginamo', 'not allowed')]:
            self.assertContains(self.post_profile('ana', bad), text, msg_prefix=bad)
        self.users['ana'].refresh_from_db()
        self.assertEqual(self.users['ana'].username, 'ana')

    def test_keeping_the_same_username_is_fine_and_other_fields_still_save(self):
        resp = self.post_profile('ana', 'ana', phone='09998887777')
        self.assertEqual(resp.status_code, 302)
        self.members['ana'].refresh_from_db()
        self.assertEqual(self.members['ana'].phone, '09998887777')
        self.assertFalse(AuditLog.objects.filter(action='CHANGE_USERNAME').exists())

    def test_librarian_can_change_a_members_username(self):
        c = Client(); c.force_login(self.lib)
        data = {'username': 'ben.new', 'first_name': 'Ben', 'last_name': 'Cruz', 'email': 'ben@x.com', 'phone': '0917',
                'address': '', 'status': 'ACTIVE', 'membership_type': 'STUDENT'}
        c.post(f"/members/{self.members['ben'].id}/edit/", data)
        self.users['ben'].refresh_from_db()
        self.assertEqual(self.users['ben'].username, 'ben.new')
        self.assertTrue(AuditLog.objects.filter(action='CHANGE_USERNAME', details__contains='by lib').exists())

    def test_librarian_edit_with_taken_username_saves_nothing(self):
        c = Client(); c.force_login(self.lib)
        data = {'username': 'ana', 'first_name': 'Changed', 'last_name': 'Cruz', 'email': 'ben@x.com', 'phone': '0917',
                'address': '', 'status': 'ACTIVE', 'membership_type': 'STUDENT'}
        c.post(f"/members/{self.members['ben'].id}/edit/", data)
        self.users['ben'].refresh_from_db(); self.members['ben'].refresh_from_db()
        self.assertEqual((self.users['ben'].username, self.members['ben'].first_name), ('ben', 'Ben'))

    def test_username_field_is_on_my_profile_and_the_staff_edit_form(self):
        c = Client(); c.force_login(self.users['ana'])
        self.assertContains(c.get('/my-profile/'), 'name="username"')
        s = Client(); s.force_login(self.lib)
        self.assertContains(s.get(f"/members/{self.members['ben'].id}/"), 'name="username"')


class AdminAccountTests(AdminFeatureBase):
    def post_details(self, **extra):
        data = {'action': 'details', 'username': 'boss', 'first_name': 'Bea', 'middle_name': 'Lim',
                'last_name': 'Santos', 'phone': '09171234567', 'email': 'bea@example.com'}
        data.update(extra)
        return self.ca.post('/management/account/', data)

    def test_only_admin_can_open_the_page(self):
        self.assertEqual(self.ca.get('/management/account/').status_code, 200)
        self.assertEqual(self.cl.get('/management/account/').status_code, 403)
        self.assertEqual(self.cm.get('/management/account/').status_code, 403)

    def test_admin_updates_username_name_and_contact_info(self):
        resp = self.post_details(username='bea.santos')
        self.assertEqual(resp.status_code, 302)
        self.admin.refresh_from_db()
        self.admin.profile.refresh_from_db()
        self.assertEqual((self.admin.username, self.admin.first_name, self.admin.last_name, self.admin.email),
                         ('bea.santos', 'Bea', 'Santos', 'bea@example.com'))
        self.assertEqual((self.admin.profile.middle_name, self.admin.profile.phone), ('Lim', '09171234567'))
        self.assertTrue(AuditLog.objects.filter(action='CHANGE_USERNAME').exists())
        self.assertEqual(self.ca.get('/management/account/').status_code, 200)  # still signed in

    def test_rejects_taken_username_and_email_and_bad_phone(self):
        for extra, text in [({'username': 'LIB'}, 'already taken'),
                            ({'email': 'S@example.com'}, 'already used'),
                            ({'phone': '12345'}, 'Philippine mobile number')]:
            self.assertContains(self.post_details(**extra), text)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.username, 'boss')

    def test_admin_changes_password_and_stays_signed_in(self):
        resp = self.ca.post('/management/account/', {
            'action': 'password', 'old_password': 'Str0ng-pass-1',
            'new_password1': 'N3w-secret-pass', 'new_password2': 'N3w-secret-pass'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Client().login(username='boss', password='N3w-secret-pass'))
        self.assertEqual(self.ca.get('/management/account/').status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action='CHANGE_ADMIN_PASSWORD').exists())

    def test_wrong_current_password_changes_nothing(self):
        resp = self.ca.post('/management/account/', {
            'action': 'password', 'old_password': 'nope',
            'new_password1': 'N3w-secret-pass', 'new_password2': 'N3w-secret-pass'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Client().login(username='boss', password='Str0ng-pass-1'))

