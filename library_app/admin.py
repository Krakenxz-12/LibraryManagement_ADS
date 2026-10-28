from django.contrib import admin
from .models import UserProfile, Category, Member, Book, BorrowTransaction, Reservation, FinePayment, Notification, AuditLog

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'phone')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email', 'phone')

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'created_at')
    search_fields = ('name',)

@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ('member_id', 'first_name', 'last_name', 'email', 'phone', 'status', 'balance', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('member_id', 'first_name', 'last_name', 'email', 'phone')

@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ('title', 'author', 'isbn', 'category', 'shelf_number', 'available_copies', 'total_copies', 'status')
    list_filter = ('status', 'category')
    search_fields = ('title', 'author', 'isbn')

@admin.register(BorrowTransaction)
class BorrowTransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_code', 'book', 'member', 'issue_date', 'due_date', 'return_date', 'status', 'fine_amount', 'fine_paid')
    list_filter = ('status', 'fine_paid', 'issue_date')
    search_fields = ('transaction_code', 'book__title', 'member__first_name', 'member__last_name', 'member__member_id')

@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ('reservation_code', 'book', 'member', 'reservation_date', 'status', 'notified_at')
    list_filter = ('status', 'reservation_date')
    search_fields = ('reservation_code', 'book__title', 'member__member_id')

@admin.register(FinePayment)
class FinePaymentAdmin(admin.ModelAdmin):
    list_display = ('receipt_number', 'member', 'amount_paid', 'payment_method', 'payment_date', 'collected_by')
    list_filter = ('payment_method', 'payment_date')
    search_fields = ('receipt_number', 'member__member_id', 'member__first_name')

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'recipient', 'notification_type', 'is_read', 'created_at')
    list_filter = ('notification_type', 'is_read')

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action', 'model_name', 'object_id')
    list_filter = ('action', 'model_name', 'timestamp')
