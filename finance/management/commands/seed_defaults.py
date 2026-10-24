from datetime import date

from django.core.management.base import BaseCommand

from finance.models import Account, Category, CompanySettings, PersonalCategory


EXPENSE_CATEGORIES = [
    ("Salaries", "Staff salaries and wages"),
    ("Office Rent", "Office or shop rent"),
    ("Electricity", "Electricity bills"),
    ("Water", "Water charges"),
    ("Internet and Telephone", "Internet, mobile, and telephone"),
    ("Fuel", "Vehicle and generator fuel"),
    ("Vehicle Maintenance", "Servicing and vehicle repairs"),
    ("Packaging", "Packaging materials"),
    ("Marketing", "Advertising and promotions"),
    ("Website and Hosting", "Website, domain, and hosting"),
    ("Software Subscriptions", "SaaS and software tools"),
    ("Office Supplies", "Stationery and consumables"),
    ("Repairs and Maintenance", "General repairs"),
    ("Bank Charges", "Bank fees and charges"),
    ("Miscellaneous Expenses", "Other operating expenses"),
    ("Loan EMI", "Loan EMI payments (full EMI counted as expense)"),
]

INCOME_CATEGORIES = [
    ("Service Income", "Income from services"),
    ("Sales Income", "Income from sales"),
    ("Delivery Charges", "Delivery fees collected"),
    ("Commission Income", "Commission received"),
    ("Other Income", "Other business income"),
]

PERSONAL_EXPENSE_CATEGORIES = [
    "Food & Dining", "Groceries", "Fuel & Transport", "Rent", "Utilities & Bills", "Shopping",
    "Medical", "Entertainment", "Education", "Travel", "Family", "Miscellaneous",
]

PERSONAL_INCOME_CATEGORIES = ["Salary / Drawings", "Interest", "Gifts", "Other Income"]


class Command(BaseCommand):
    help = "Create default company settings, categories, and sample accounts if they do not exist."

    def handle(self, *args, **options):
        CompanySettings.get_solo()
        created_cats = 0
        for name, description in EXPENSE_CATEGORIES:
            _, created = Category.objects.get_or_create(
                name=name,
                category_type=Category.EXPENSE,
                defaults={"description": description, "is_active": True},
            )
            created_cats += int(created)
        for name, description in INCOME_CATEGORIES:
            _, created = Category.objects.get_or_create(
                name=name,
                category_type=Category.INCOME,
                defaults={"description": description, "is_active": True},
            )
            created_cats += int(created)
        for names, category_type in (
            (PERSONAL_EXPENSE_CATEGORIES, Category.EXPENSE),
            (PERSONAL_INCOME_CATEGORIES, Category.INCOME),
        ):
            for name in names:
                _, created = PersonalCategory.objects.get_or_create(name=name, category_type=category_type)
                created_cats += int(created)

        sample_accounts = [
            ("SBI Current Account", Account.BANK, "State Bank of India"),
            ("HDFC Current Account", Account.BANK, "HDFC Bank"),
            ("Cash", Account.CASH, ""),
            ("Petty Cash", Account.PETTY_CASH, ""),
            ("UPI Account", Account.UPI, ""),
        ]
        created_accounts = 0
        for name, account_type, bank_name in sample_accounts:
            _, created = Account.objects.get_or_create(
                name=name,
                defaults={
                    "account_type": account_type,
                    "bank_name": bank_name,
                    "opening_balance": 0,
                    "opening_balance_date": date(2026, 1, 1),
                    "is_active": True,
                },
            )
            created_accounts += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Defaults ready. Categories added: {created_cats}. Accounts added: {created_accounts}."
            )
        )
