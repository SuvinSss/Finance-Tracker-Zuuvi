from django.contrib import admin

from .models import (
    Account,
    Budget,
    Category,
    CompanySettings,
    Loan,
    PersonalCategory,
    PersonalTransaction,
    Transaction,
)


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    list_display = ("company_name", "currency", "budget_warning_threshold")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "category_type", "is_active")
    list_filter = ("category_type", "is_active")
    search_fields = ("name",)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_type", "opening_balance", "opening_balance_date", "is_active")
    list_filter = ("account_type", "is_active")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "entry_type", "amount", "category", "account", "kind", "description")
    list_filter = ("entry_type", "kind", "account")
    search_fields = ("description", "reference_number")


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("name", "lender", "emi_amount", "emi_day", "first_emi_month", "tenure_months", "is_active")
    list_filter = ("is_active",)


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ("year", "month", "category", "amount")
    list_filter = ("year", "month")


@admin.register(PersonalCategory)
class PersonalCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "category_type", "is_active")
    list_filter = ("category_type", "is_active")


@admin.register(PersonalTransaction)
class PersonalTransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "entry_type", "amount", "category", "payment_mode", "description")
    list_filter = ("entry_type", "payment_mode", "category")
    search_fields = ("description",)
