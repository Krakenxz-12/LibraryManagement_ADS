from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta, date
from decimal import Decimal

from library_app.models import (
    UserProfile, Category, Member, Book,
    BorrowTransaction, Reservation, FinePayment, Notification
)

class Command(BaseCommand):
    help = 'Seeds initial users, categories, books, members, transactions, and alerts for testing & demonstration.'

    def handle(self, *args, **kwargs):
        self.stdout.write("Starting database seeding...")

        # 1. Create Default Users (Admin, Librarian, Member)
        users_data = [
            ('admin', 'admin123', 'System', 'Administrator', 'admin@library.edu', 'ADMIN'),
            ('librarian', 'librarian123', 'Sarah', 'Jenkins', 'sjenkins@library.edu', 'LIBRARIAN'),
            ('member', 'member123', 'Alex', 'Morgan', 'alex.morgan@student.edu', 'MEMBER'),
        ]

        for username, password, fn, ln, email, role in users_data:
            user, created = User.objects.get_or_create(username=username, defaults={
                'email': email,
                'first_name': fn,
                'last_name': ln,
                'is_staff': True if role in ['ADMIN', 'LIBRARIAN'] else False,
                'is_superuser': True if role == 'ADMIN' else False,
            })
            if created:
                user.set_password(password)
                user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user, defaults={'role': role, 'phone': '+1 (555) 019-2834'})
            profile.role = role
            profile.save()

        admin_user = User.objects.get(username='admin')
        librarian_user = User.objects.get(username='librarian')
        member_user = User.objects.get(username='member')

        # 2. Create Categories
        categories_data = [
            ("Computer Science", "Algorithms, software engineering, databases, and AI systems"),
            ("Mathematics", "Calculus, linear algebra, discrete math, and statistics"),
            ("Literature & Fiction", "Classic novels, contemporary prose, and poetry collections"),
            ("Science & Physics", "Theoretical mechanics, quantum dynamics, and astronomy"),
            ("History & Politics", "World history, government theory, and diplomacy"),
        ]
        cat_objs = {}
        for name, desc in categories_data:
            cat, _ = Category.objects.get_or_create(name=name, defaults={'description': desc})
            cat_objs[name] = cat

        # 3. Create Library Members
        members_data = [
            ("Alex", "Morgan", "alex.morgan@student.edu", "+1-555-0101", "402 Dormitory Hall A", date(2003, 5, 14), member_user),
            ("Elena", "Rostova", "elena.r@student.edu", "+1-555-0102", "12 University Ave", date(2002, 11, 23), None),
            ("Marcus", "Chen", "marcus.c@student.edu", "+1-555-0103", "88 Oak Crest Blvd", date(2001, 8, 30), None),
            ("Sophia", "Patel", "sophia.p@student.edu", "+1-555-0104", "104 Maple Street", date(2004, 2, 19), None),
            ("David", "Kim", "david.k@student.edu", "+1-555-0105", "300 Pine Road", date(2002, 7, 7), None),
        ]
        created_members = []
        for i, (fn, ln, email, phone, addr, dob, linked_u) in enumerate(members_data, 1):
            mem_id = f"MEM-2026-{i:04d}"
            mem, _ = Member.objects.get_or_create(
                member_id=mem_id,
                defaults={
                    'user': linked_u,
                    'first_name': fn,
                    'last_name': ln,
                    'email': email,
                    'phone': phone,
                    'address': addr,
                    'date_of_birth': dob,
                    'status': 'ACTIVE',
                    'balance': Decimal('0.00')
                }
            )
            created_members.append(mem)

        # 4. Create Books Catalog with High-Quality Covers
        books_data = [
            ("The Love Hypothesis", "Ali Hazelwood", "978-0593336823", "Literature & Fiction", "LIT-303", 2021, 5,
             "https://images-na.ssl-images-amazon.com/images/S/compressed.photo.goodreads.com/books/1611937942i/56732449.jpg"),
            ("Alice's Adventures in Wonderland", "Lewis Carroll", "978-0141439761", "Literature & Fiction", "LIT-304", 1865, 4,
             "https://images-na.ssl-images-amazon.com/images/S/compressed.photo.goodreads.com/books/1327872220i/24213.jpg"),
            ("Clean Code: A Handbook of Agile Software Craftsmanship", "Robert C. Martin", "978-0132350884", "Computer Science", "CS-101", 2008, 4,
             "https://covers.openlibrary.org/b/isbn/9780132350884-L.jpg"),
            ("Introduction to Algorithms (CLRS 4th Ed.)", "Thomas H. Cormen", "978-0262046305", "Computer Science", "CS-102", 2022, 3,
             "https://covers.openlibrary.org/b/isbn/9780262046305-L.jpg"),
            ("Designing Data-Intensive Applications", "Martin Kleppmann", "978-1449373320", "Computer Science", "CS-103", 2017, 3,
             "https://covers.openlibrary.org/b/isbn/9781449373320-L.jpg"),
            ("Linear Algebra and Its Applications", "Gilbert Strang", "978-0030105678", "Mathematics", "MATH-201", 2016, 2,
             "https://covers.openlibrary.org/b/isbn/9780030105678-L.jpg"),
            ("To Kill a Mockingbird", "Harper Lee", "978-0061120084", "Literature & Fiction", "LIT-301", 1960, 3,
             "https://covers.openlibrary.org/b/isbn/9780061120084-L.jpg"),
            ("1984: A Novel", "George Orwell", "978-0451524935", "Literature & Fiction", "LIT-302", 1949, 2,
             "https://covers.openlibrary.org/b/isbn/9780451524935-L.jpg"),
            ("Sapiens: A Brief History of Humankind", "Yuval Noah Harari", "978-0062316097", "History & Politics", "HIST-501", 2014, 3,
             "https://covers.openlibrary.org/b/isbn/9780062316097-L.jpg"),
        ]

        created_books = []
        for title, author, isbn, cat_name, shelf, year, copies, cover in books_data:
            book, created = Book.objects.get_or_create(
                isbn=isbn,
                defaults={
                    'title': title,
                    'author': author,
                    'category': cat_objs[cat_name],
                    'shelf_number': shelf,
                    'publication_year': year,
                    'total_copies': copies,
                    'available_copies': copies,
                    'status': 'AVAILABLE',
                    'cover_image_url': cover,
                    'description': f"Comprehensive resource in {cat_name} authored by {author}."
                }
            )
            if not created and not book.cover_image_url:
                book.cover_image_url = cover
                book.save()
            created_books.append(book)

        today = timezone.now().date()

        # 5. Create Sample Transactions:
        # Txn 1: The Love Hypothesis (Alex Morgan, 6% progress)
        b_love = created_books[0] # The Love Hypothesis
        m1 = created_members[0] # Alex Morgan
        t1, _ = BorrowTransaction.objects.get_or_create(
            transaction_code="TXN-2026-0001",
            defaults={
                'member': m1,
                'book': b_love,
                'issued_by': librarian_user,
                'issue_date': today - timedelta(days=3),
                'due_date': today + timedelta(days=11),
                'reading_progress': 6,
                'status': 'BORROWED'
            }
        )
        b_love.available_copies = max(0, b_love.available_copies - 1)
        b_love.save()

        # Txn for Alice's Adventures in Wonderland (Alex Morgan, 0% progress)
        b_alice = created_books[1] # Alice's Adventures in Wonderland
        t_alice, _ = BorrowTransaction.objects.get_or_create(
            transaction_code="TXN-2026-0004",
            defaults={
                'member': m1,
                'book': b_alice,
                'issued_by': librarian_user,
                'issue_date': today - timedelta(days=1),
                'due_date': today + timedelta(days=13),
                'reading_progress': 0,
                'status': 'BORROWED'
            }
        )
        b_alice.available_copies = max(0, b_alice.available_copies - 1)
        b_alice.save()

        # Txn 2: Overdue Borrow (Elena Rostova - 4 days overdue, fine $2.00)
        b2 = created_books[1] # CLRS
        m2 = created_members[1] # Elena
        t2, _ = BorrowTransaction.objects.get_or_create(
            transaction_code="TXN-2026-0002",
            defaults={
                'member': m2,
                'book': b2,
                'issued_by': librarian_user,
                'issue_date': today - timedelta(days=18),
                'due_date': today - timedelta(days=4),
                'status': 'OVERDUE',
                'fine_amount': Decimal('80.00'),
                'fine_paid': False
            }
        )
        m2.balance = Decimal('80.00')
        m2.save()
        b2.available_copies = max(0, b2.available_copies - 1)
        b2.save()

        # Txn 3: Returned book with fine paid
        b3 = created_books[2] # DDIA
        m3 = created_members[2] # Marcus
        t3, _ = BorrowTransaction.objects.get_or_create(
            transaction_code="TXN-2026-0003",
            defaults={
                'member': m3,
                'book': b3,
                'issued_by': librarian_user,
                'returned_to': librarian_user,
                'issue_date': today - timedelta(days=25),
                'due_date': today - timedelta(days=11),
                'return_date': today - timedelta(days=9), # 2 days late
                'status': 'RETURNED',
                'fine_amount': Decimal('40.00'),
                'fine_paid': True
            }
        )
        FinePayment.objects.get_or_create(
            receipt_number="REC-2026-0001",
            defaults={
                'transaction': t3,
                'member': m3,
                'amount_paid': Decimal('40.00'),
                'payment_method': 'CASH',
                'collected_by': librarian_user,
                'notes': "Overdue fine paid upon returning DDIA book."
            }
        )

        # 6. Create Reservation (Marcus reserves CLRS which is overdue by Elena)
        Reservation.objects.get_or_create(
            reservation_code="RES-2026-0001",
            defaults={
                'member': m3,
                'book': b2,
                'status': 'PENDING',
                'notes': "Waiting for return copy."
            }
        )

        # 7. Create System Notifications
        Notification.objects.get_or_create(
            recipient=member_user,
            title="Due Date Reminder: 'Clean Code'",
            notification_type='DUE_DATE',
            defaults={
                'message': f"Your borrowed book 'Clean Code' is due in 9 days on {(today + timedelta(days=9)).strftime('%b %d, %Y')}."
            }
        )
        Notification.objects.get_or_create(
            recipient=librarian_user,
            title="System Alert: Overdue Books Detected",
            notification_type='LIBRARIAN_ALERT',
            defaults={
                'message': "There is 1 unreturned book overdue (CLRS 4th Ed.). Elena Rostova has been flagged."
            }
        )

        self.stdout.write(self.style.SUCCESS("Database seeded successfully with ready-to-demo data!"))
