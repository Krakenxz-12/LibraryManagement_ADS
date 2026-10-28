from django.urls import path
from . import views

urlpatterns = [
    # Auth & Home
    path('', views.dashboard_view, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Module 1: Members
    path('members/', views.member_list_view, name='member_list'),
    path('members/new/', views.member_create_view, name='member_create'),
    path('members/<int:member_id>/', views.member_detail_view, name='member_detail'),
    path('members/<int:member_id>/edit/', views.member_edit_view, name='member_edit'),
    path('members/<int:member_id>/toggle-status/', views.member_toggle_status_view, name='member_toggle_status'),

    # Module 2 & 8: Books Catalog & Search
    path('books/', views.book_list_view, name='book_list'),
    path('books/new/', views.book_create_view, name='book_create'),
    path('books/<int:book_id>/', views.book_detail_view, name='book_detail'),
    path('books/<int:book_id>/edit/', views.book_edit_view, name='book_edit'),
    path('books/<int:book_id>/delete/', views.book_delete_view, name='book_delete'),

    # Module 3: Borrowing & Returning
    path('transactions/', views.transaction_list_view, name='transaction_list'),
    path('transactions/borrow/', views.borrow_create_view, name='borrow_create'),
    path('transactions/<int:transaction_id>/return/', views.return_process_view, name='return_process'),

    # Module 4: Reservations
    path('reservations/', views.reservation_list_view, name='reservation_list'),
    path('books/<int:book_id>/reserve/', views.reserve_book_view, name='reserve_book'),
    path('reservations/<int:reservation_id>/cancel/', views.cancel_reservation_view, name='cancel_reservation'),

    # Module 5: Fines & Payments
    path('fines/', views.fines_list_view, name='fines_list'),
    path('transactions/<int:transaction_id>/pay-fine/', views.process_fine_payment_view, name='process_fine_payment'),
    path('receipts/<str:receipt_number>/', views.receipt_view, name='receipt_view'),
    path('receipts/<str:receipt_number>/pdf/', views.receipt_download_pdf_view, name='receipt_download_pdf'),

    # Module 6: Reports & Statistics
    path('reports/', views.reports_dashboard_view, name='reports_dashboard'),
    path('reports/export/', views.export_report_view, name='export_report'),

    # Module 7 & 10: Users, Backups & Security
    path('management/users/', views.user_management_view, name='user_management'),
    path('management/backup/download/', views.backup_download_view, name='backup_download'),
    path('management/backup/restore/', views.backup_restore_view, name='backup_restore'),

    # Module 9: Notifications
    path('notifications/', views.notification_list_view, name='notification_list'),
    path('notifications/<int:notification_id>/read/', views.mark_notification_read_view, name='mark_notification_read'),
    path('notifications/read-all/', views.mark_all_notifications_read_view, name='mark_all_notifications_read'),
]
