from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

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
from .services import (
    EMI_DUE,
    STATUS_FULL,
    STATUS_NEAR,
    STATUS_OVER,
    STATUS_UNBUDGETED,
    STATUS_WITHIN,
    account_balance,
    budget_vs_actual,
    create_transfer,
    dashboard_summary,
    ensure_emi_category,
    expenses_by_category,
    income_expense_totals,
    loan_status,
    personal_by_category,
    personal_totals,
    record_emi_payment,
)


class FinanceBaseTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="test-pass-123")
        self.expense_fuel = Category.objects.create(name="Fuel", category_type=Category.EXPENSE)
        self.expense_salary = Category.objects.create(name="Salaries", category_type=Category.EXPENSE)
        self.expense_rent = Category.objects.create(name="Office Rent", category_type=Category.EXPENSE)
        self.income_sales = Category.objects.create(name="Sales Income", category_type=Category.INCOME)
        self.sbi = Account.objects.create(
            name="SBI Current Account",
            account_type=Account.BANK,
            bank_name="SBI",
            opening_balance=Decimal("100000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        self.hdfc = Account.objects.create(
            name="HDFC Current Account",
            account_type=Account.BANK,
            bank_name="HDFC",
            opening_balance=Decimal("50000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        self.cash = Account.objects.create(
            name="Cash",
            account_type=Account.CASH,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 10, 1),
        )

    def login(self):
        self.client.login(username="owner", password="test-pass-123")

    def add_txn(self, **kwargs):
        defaults = {
            "date": date(2026, 10, 7),
            "entry_type": Transaction.DEBIT,
            "amount": Decimal("2500.00"),
            "category": self.expense_fuel,
            "account": self.sbi,
            "description": "Delivery vehicle fuel",
            "kind": Transaction.KIND_OPERATING,
        }
        defaults.update(kwargs)
        txn = Transaction(**defaults)
        txn.full_clean()
        txn.save()
        return txn


class AuthTests(FinanceBaseTestCase):
    def test_login_success_and_failure(self):
        response = self.client.post(reverse("login"), {"username": "owner", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid username and password")

        response = self.client.post(reverse("login"), {"username": "owner", "password": "test-pass-123"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("finance:dashboard"))

    def test_protected_pages_require_login(self):
        for name in [
            "finance:dashboard",
            "finance:transaction_list",
            "finance:budget_list",
            "finance:account_list",
            "finance:category_list",
            "finance:reports",
            "finance:settings",
        ]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response.url)


class BudgetTests(FinanceBaseTestCase):
    def test_create_and_edit_budget(self):
        self.login()
        response = self.client.post(
            reverse("finance:budget_create"),
            {
                "year": 2026,
                "month": 10,
                "category": self.expense_fuel.pk,
                "amount": "25000.00",
                "notes": "October fuel",
            },
        )
        self.assertEqual(response.status_code, 302)
        budget = Budget.objects.get()
        self.assertEqual(budget.amount, Decimal("25000.00"))

        response = self.client.post(
            reverse("finance:budget_edit", args=[budget.pk]),
            {
                "year": 2026,
                "month": 10,
                "category": self.expense_fuel.pk,
                "amount": "27000.00",
                "notes": "Revised",
            },
        )
        self.assertEqual(response.status_code, 302)
        budget.refresh_from_db()
        self.assertEqual(budget.amount, Decimal("27000.00"))

    def test_duplicate_category_month_budget_rejected(self):
        Budget.objects.create(year=2026, month=10, category=self.expense_fuel, amount=Decimal("25000"))
        self.login()
        response = self.client.post(
            reverse("finance:budget_create"),
            {
                "year": 2026,
                "month": 10,
                "category": self.expense_fuel.pk,
                "amount": "1000.00",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Budget.objects.count(), 1)
        self.assertContains(response, "already exists")


class TransactionAndBudgetCalcTests(FinanceBaseTestCase):
    def test_debit_and_credit_transactions(self):
        debit = self.add_txn()
        credit = self.add_txn(
            entry_type=Transaction.CREDIT,
            amount=Decimal("15000.00"),
            category=self.income_sales,
            account=self.hdfc,
            description="Customer payment",
        )
        self.assertEqual(debit.entry_type, Transaction.DEBIT)
        self.assertEqual(credit.entry_type, Transaction.CREDIT)
        totals = income_expense_totals(2026, 10)
        self.assertEqual(totals["income"], Decimal("15000.00"))
        self.assertEqual(totals["expenses"], Decimal("2500.00"))
        self.assertEqual(totals["net_cash_flow"], Decimal("12500.00"))

    def test_zero_or_negative_amount_rejected(self):
        txn = Transaction(
            date=date(2026, 10, 7),
            entry_type=Transaction.DEBIT,
            amount=Decimal("0.00"),
            category=self.expense_fuel,
            account=self.sbi,
            description="Invalid",
        )
        with self.assertRaises(ValidationError):
            txn.full_clean()

    def test_budget_remaining_and_statuses(self):
        Budget.objects.create(year=2026, month=10, category=self.expense_salary, amount=Decimal("80000"))
        Budget.objects.create(year=2026, month=10, category=self.expense_fuel, amount=Decimal("25000"))
        Budget.objects.create(year=2026, month=10, category=self.expense_rent, amount=Decimal("20000"))
        marketing = Category.objects.create(name="Marketing", category_type=Category.EXPENSE)
        Budget.objects.create(year=2026, month=10, category=marketing, amount=Decimal("15000"))
        unbudgeted = Category.objects.create(name="Packaging", category_type=Category.EXPENSE)

        self.add_txn(category=self.expense_salary, amount=Decimal("75000"), description="Salary")
        self.add_txn(category=self.expense_fuel, amount=Decimal("27500"), description="Fuel")
        self.add_txn(category=self.expense_rent, amount=Decimal("20000"), description="Rent")
        self.add_txn(category=marketing, amount=Decimal("8000"), description="Ads")
        self.add_txn(category=unbudgeted, amount=Decimal("1200"), description="Boxes")

        CompanySettings.get_solo()
        bva = budget_vs_actual(2026, 10, threshold=90)
        by_name = {row["category"]: row for row in bva["rows"]}
        self.assertEqual(by_name["Salaries"]["status"], STATUS_NEAR)
        self.assertEqual(by_name["Salaries"]["remaining"], Decimal("5000.00"))
        self.assertEqual(by_name["Fuel"]["status"], STATUS_OVER)
        self.assertEqual(by_name["Fuel"]["remaining"], Decimal("-2500.00"))
        self.assertEqual(by_name["Office Rent"]["status"], STATUS_FULL)
        self.assertEqual(by_name["Marketing"]["status"], STATUS_WITHIN)
        self.assertEqual(bva["unbudgeted"][0]["status"], STATUS_UNBUDGETED)
        self.assertEqual(bva["unbudgeted"][0]["actual"], Decimal("1200.00"))

    def test_month_filtering(self):
        self.add_txn(date=date(2026, 10, 5), amount=Decimal("1000"))
        self.add_txn(date=date(2026, 11, 5), amount=Decimal("4000"))
        self.assertEqual(income_expense_totals(2026, 10)["expenses"], Decimal("1000.00"))
        self.assertEqual(income_expense_totals(2026, 11)["expenses"], Decimal("4000.00"))


class AccountAndTransferTests(FinanceBaseTestCase):
    def test_account_balance_formula_and_opening_date(self):
        self.add_txn(amount=Decimal("2500"))
        self.add_txn(
            entry_type=Transaction.CREDIT,
            amount=Decimal("15000"),
            category=self.income_sales,
            description="Sale",
        )
        self.add_txn(
            date=date(2026, 9, 20),
            amount=Decimal("9999"),
            description="Before opening date",
        )
        self.assertEqual(account_balance(self.sbi), Decimal("112500.00"))

    def test_internal_transfer_moves_money_without_affecting_pnl(self):
        Budget.objects.create(year=2026, month=10, category=self.expense_fuel, amount=Decimal("25000"))
        create_transfer(
            date=date(2026, 10, 8),
            amount=Decimal("10000"),
            source=self.sbi,
            destination=self.hdfc,
            description="Move to HDFC",
        )
        self.assertEqual(account_balance(self.sbi), Decimal("90000.00"))
        self.assertEqual(account_balance(self.hdfc), Decimal("60000.00"))
        totals = income_expense_totals(2026, 10)
        self.assertEqual(totals["income"], Decimal("0.00"))
        self.assertEqual(totals["expenses"], Decimal("0.00"))
        bva = budget_vs_actual(2026, 10)
        self.assertEqual(bva["total_actual"], Decimal("0.00"))
        self.assertEqual(Transaction.objects.filter(kind=Transaction.KIND_TRANSFER).count(), 2)

    def test_transfer_same_account_rejected(self):
        with self.assertRaises(ValueError):
            create_transfer(
                date=date(2026, 10, 8),
                amount=Decimal("100"),
                source=self.sbi,
                destination=self.sbi,
                description="Invalid",
            )

    def test_edit_and_delete_transaction_recalculates(self):
        txn = self.add_txn(amount=Decimal("2500"))
        self.assertEqual(account_balance(self.sbi), Decimal("97500.00"))
        txn.amount = Decimal("5000")
        txn.full_clean()
        txn.save()
        self.assertEqual(account_balance(self.sbi), Decimal("95000.00"))
        txn.delete()
        self.assertEqual(account_balance(self.sbi), Decimal("100000.00"))


class CategoryValidationTests(FinanceBaseTestCase):
    def test_income_category_cannot_be_used_for_debit(self):
        txn = Transaction(
            date=date(2026, 10, 7),
            entry_type=Transaction.DEBIT,
            amount=Decimal("100"),
            category=self.income_sales,
            account=self.sbi,
            description="Wrong type",
        )
        with self.assertRaises(ValidationError):
            txn.full_clean()

    def test_expense_category_cannot_be_used_for_credit(self):
        txn = Transaction(
            date=date(2026, 10, 7),
            entry_type=Transaction.CREDIT,
            amount=Decimal("100"),
            category=self.expense_fuel,
            account=self.sbi,
            description="Wrong type",
        )
        with self.assertRaises(ValidationError):
            txn.full_clean()

    def test_referenced_category_is_deactivated_not_deleted(self):
        self.login()
        self.add_txn()
        response = self.client.post(reverse("finance:category_delete", args=[self.expense_fuel.pk]))
        self.assertEqual(response.status_code, 302)
        self.expense_fuel.refresh_from_db()
        self.assertFalse(self.expense_fuel.is_active)
        self.assertTrue(Category.objects.filter(pk=self.expense_fuel.pk).exists())


class CategoryAndAccountTests(FinanceBaseTestCase):
    def category_data(self, **overrides):
        data = {"name": "Packaging", "category_type": Category.EXPENSE, "description": "", "is_active": "on"}
        data.update(overrides)
        return data

    def test_create_category_and_duplicate_rules(self):
        self.login()
        response = self.client.post(reverse("finance:category_create"), self.category_data())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Category.objects.filter(name="Packaging", category_type=Category.EXPENSE).exists())

        response = self.client.post(reverse("finance:category_create"), self.category_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Category.objects.filter(name="Packaging").count(), 1)

        response = self.client.post(
            reverse("finance:category_create"), self.category_data(category_type=Category.INCOME)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Category.objects.filter(name="Packaging").count(), 2)

    def test_type_change_blocked_when_category_in_use(self):
        self.login()
        Budget.objects.create(year=2026, month=10, category=self.expense_fuel, amount=Decimal("5000"))
        response = self.client.post(
            reverse("finance:category_edit", args=[self.expense_fuel.pk]),
            self.category_data(name="Fuel", category_type=Category.INCOME),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "type can&#x27;t be changed")
        self.expense_fuel.refresh_from_db()
        self.assertEqual(self.expense_fuel.category_type, Category.EXPENSE)

        response = self.client.post(
            reverse("finance:category_edit", args=[self.expense_rent.pk]),
            self.category_data(name="Office Rent", category_type=Category.INCOME),
        )
        self.assertEqual(response.status_code, 302)
        self.expense_rent.refresh_from_db()
        self.assertEqual(self.expense_rent.category_type, Category.INCOME)

    def test_category_with_only_budget_is_deactivated_not_deleted(self):
        self.login()
        Budget.objects.create(year=2026, month=10, category=self.expense_salary, amount=Decimal("5000"))
        response = self.client.post(reverse("finance:category_delete", args=[self.expense_salary.pk]))
        self.assertEqual(response.status_code, 302)
        self.expense_salary.refresh_from_db()
        self.assertFalse(self.expense_salary.is_active)

    def test_unused_category_is_deleted(self):
        self.login()
        response = self.client.post(reverse("finance:category_delete", args=[self.expense_rent.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Category.objects.filter(pk=self.expense_rent.pk).exists())

    def test_create_account_shows_opening_balance(self):
        self.login()
        response = self.client.post(
            reverse("finance:account_create"),
            {
                "name": "Petty Cash",
                "account_type": Account.PETTY_CASH,
                "bank_name": "",
                "account_number": "",
                "opening_balance": "2500.00",
                "opening_balance_date": "2026-10-01",
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        account = Account.objects.get(name="Petty Cash")
        self.assertEqual(account_balance(account), Decimal("2500.00"))
        response = self.client.get(reverse("finance:account_list"))
        self.assertContains(response, "Petty Cash")
        self.assertContains(response, "₹2,500.00")

    def test_account_with_transactions_is_deactivated_unused_is_deleted(self):
        self.login()
        self.add_txn(account=self.sbi)
        self.client.post(reverse("finance:account_delete", args=[self.sbi.pk]))
        self.sbi.refresh_from_db()
        self.assertFalse(self.sbi.is_active)

        self.client.post(reverse("finance:account_delete", args=[self.cash.pk]))
        self.assertFalse(Account.objects.filter(pk=self.cash.pk).exists())

    def test_logout_ends_session(self):
        self.login()
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))
        response = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(response.status_code, 302)


class LoanTests(FinanceBaseTestCase):
    def setUp(self):
        super().setUp()
        self.loan = Loan.objects.create(
            name="Delivery van loan",
            lender="SBI",
            loan_amount=Decimal("500000"),
            emi_amount=Decimal("12000"),
            emi_day=5,
            first_emi_month=date(2026, 9, 1),
            tenure_months=24,
            repayment_account=self.sbi,
        )

    def pay(self, emi_month, **kwargs):
        defaults = {
            "loan": self.loan,
            "emi_month": emi_month,
            "date": emi_month.replace(day=5),
            "amount": Decimal("12000"),
            "account": self.sbi,
        }
        defaults.update(kwargs)
        return record_emi_payment(**defaults)

    def test_create_loan_via_view(self):
        self.login()
        response = self.client.post(
            reverse("finance:loan_create"),
            {
                "name": "Office loan",
                "lender": "HDFC",
                "loan_amount": "100000",
                "emi_amount": "5000",
                "emi_day": "10",
                "first_emi_month": "2026-10",
                "tenure_months": "20",
                "repayment_account": self.hdfc.pk,
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        loan = Loan.objects.get(name="Office loan")
        self.assertEqual(loan.first_emi_month, date(2026, 10, 1))
        self.assertEqual(loan_status(loan, date(2026, 10, 1))["remaining_payable"], Decimal("100000.00"))

    def test_emi_payment_counts_as_expense_and_reduces_balance(self):
        Budget.objects.create(year=2026, month=10, category=ensure_emi_category(), amount=Decimal("12000"))
        self.pay(date(2026, 10, 1))
        self.assertEqual(account_balance(self.sbi), Decimal("88000.00"))
        self.assertEqual(income_expense_totals(2026, 10)["expenses"], Decimal("12000.00"))
        row = budget_vs_actual(2026, 10)["rows"][0]
        self.assertEqual(row["category"], "Loan EMI")
        self.assertEqual(row["status"], STATUS_FULL)

    def test_pay_via_view_and_duplicate_month_rejected(self):
        self.login()
        data = {"emi_month": "2026-10-01", "date": "2026-10-05", "amount": "12000", "account": self.sbi.pk}
        response = self.client.post(reverse("finance:emi_pay", args=[self.loan.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.loan.emi_payments.count(), 1)

        response = self.client.post(reverse("finance:emi_pay", args=[self.loan.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.loan.emi_payments.count(), 1)

        with self.assertRaises(ValidationError):
            self.pay(date(2026, 10, 1))

    def test_month_outside_tenure_rejected(self):
        self.login()
        response = self.client.post(
            reverse("finance:emi_pay", args=[self.loan.pk]),
            {"emi_month": "2026-08-01", "date": "2026-08-05", "amount": "12000", "account": self.sbi.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.loan.emi_payments.exists())

    def test_status_overdue_and_due(self):
        today = date(2026, 10, 10)
        status = loan_status(self.loan, today)
        self.assertEqual(status["paid_count"], 0)
        self.assertEqual([row["emi_month"] for row in status["overdue"]], [date(2026, 9, 1), date(2026, 10, 1)])
        self.assertEqual(status["remaining_payable"], Decimal("288000.00"))

        self.pay(date(2026, 9, 1))
        status = loan_status(self.loan, today)
        self.assertEqual(status["paid_count"], 1)
        self.assertEqual(status["remaining_payable"], Decimal("276000.00"))
        self.assertEqual(status["next_due"]["emi_month"], date(2026, 10, 1))
        self.assertEqual(len(status["overdue"]), 1)

        status = loan_status(self.loan, date(2026, 10, 3))
        self.assertEqual(status["overdue"], [])
        self.assertEqual(status["next_due"]["state"], EMI_DUE)

    def test_deleting_emi_transaction_reopens_month(self):
        txn = self.pay(date(2026, 9, 1))
        txn.delete()
        self.assertEqual(account_balance(self.sbi), Decimal("100000.00"))
        self.assertEqual(loan_status(self.loan, date(2026, 10, 10))["paid_count"], 0)

    def test_loan_with_payments_is_closed_not_deleted(self):
        self.login()
        self.pay(date(2026, 9, 1))
        self.client.post(reverse("finance:loan_delete", args=[self.loan.pk]))
        self.loan.refresh_from_db()
        self.assertFalse(self.loan.is_active)

        other = Loan.objects.create(
            name="Unused", lender="X", loan_amount=1, emi_amount=1, emi_day=1,
            first_emi_month=date(2026, 10, 1), tenure_months=1, repayment_account=self.hdfc,
        )
        self.client.post(reverse("finance:loan_delete", args=[other.pk]))
        self.assertFalse(Loan.objects.filter(pk=other.pk).exists())

    def test_dashboard_shows_emi_reminders_and_empty_state(self):
        self.login()
        response = self.client.get(reverse("finance:dashboard") + "?year=2026&month=10")
        self.assertContains(response, "EMI reminders")
        self.assertContains(response, "Delivery van loan")
        self.loan.delete()
        response = self.client.get(reverse("finance:dashboard"))
        self.assertContains(response, "No loans yet.")
        self.assertContains(self.client.get(reverse("finance:loan_list")), "No loans yet.")


class ReportConsistencyTests(FinanceBaseTestCase):
    def test_reports_match_underlying_transactions_and_dashboard(self):
        Budget.objects.create(year=2026, month=10, category=self.expense_fuel, amount=Decimal("25000"))
        self.add_txn(amount=Decimal("2500"))
        self.add_txn(
            entry_type=Transaction.CREDIT,
            amount=Decimal("15000"),
            category=self.income_sales,
            description="Sale",
        )
        create_transfer(
            date=date(2026, 10, 8),
            amount=Decimal("10000"),
            source=self.sbi,
            destination=self.hdfc,
            description="Internal",
        )
        dash = dashboard_summary(2026, 10)
        ie = income_expense_totals(2026, 10)
        bva = budget_vs_actual(2026, 10)
        self.assertEqual(dash["total_income"], ie["income"])
        self.assertEqual(dash["total_expenses"], ie["expenses"])
        self.assertEqual(dash["net_cash_flow"], ie["net_cash_flow"])
        self.assertEqual(dash["total_expense_budget"], bva["total_budget"])
        self.assertEqual(dash["actual_expenses"], Decimal("2500.00"))
        self.assertEqual(expenses_by_category(2026, 10)[0]["actual"], Decimal("2500.00"))
        self.assertEqual(ie["income"], Decimal("15000.00"))

        self.login()
        self.client.get(reverse("finance:dashboard") + "?year=2026&month=10")
        response = self.client.get(reverse("finance:reports") + "?year=2026&month=10")
        self.assertContains(response, "₹15,000.00")
        self.assertContains(response, "₹2,500.00")
        csv_response = self.client.get(reverse("finance:reports") + "?year=2026&month=10&export=csv&report=income")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(b"15000", csv_response.content)


class PersonalTests(FinanceBaseTestCase):
    def setUp(self):
        super().setUp()
        self.p_food, _ = PersonalCategory.objects.get_or_create(name="Food & Dining", category_type=Category.EXPENSE)
        self.p_medical, _ = PersonalCategory.objects.get_or_create(name="Medical", category_type=Category.EXPENSE)
        self.p_salary, _ = PersonalCategory.objects.get_or_create(
            name="Salary / Drawings", category_type=Category.INCOME
        )

    def add_personal(self, **kwargs):
        defaults = {
            "date": date(2026, 10, 7),
            "entry_type": Transaction.DEBIT,
            "amount": Decimal("500.00"),
            "category": self.p_food,
            "payment_mode": PersonalTransaction.UPI,
            "description": "Dinner",
        }
        defaults.update(kwargs)
        txn = PersonalTransaction(**defaults)
        txn.full_clean()
        txn.save()
        return txn

    def test_default_categories_seeded_by_migration(self):
        self.assertTrue(PersonalCategory.objects.filter(name="Groceries", category_type=Category.EXPENSE).exists())

    def test_category_must_match_entry_type(self):
        with self.assertRaises(ValidationError):
            self.add_personal(category=self.p_salary)
        with self.assertRaises(ValidationError):
            self.add_personal(entry_type=Transaction.CREDIT, category=self.p_food)
        with self.assertRaises(ValidationError):
            self.add_personal(amount=Decimal("0"))

    def test_totals_and_by_category(self):
        self.add_personal(amount=Decimal("500"))
        self.add_personal(amount=Decimal("1500"), category=self.p_medical)
        self.add_personal(entry_type=Transaction.CREDIT, amount=Decimal("40000"), category=self.p_salary)
        self.add_personal(amount=Decimal("999"), date=date(2026, 9, 30))
        totals = personal_totals(2026, 10)
        self.assertEqual(totals["income"], Decimal("40000.00"))
        self.assertEqual(totals["expenses"], Decimal("2000.00"))
        self.assertEqual(totals["net"], Decimal("38000.00"))
        rows = personal_by_category(2026, 10)
        self.assertEqual([r["category"] for r in rows], ["Medical", "Food & Dining"])
        self.assertEqual(rows[0]["share"], Decimal("75.0"))

    def test_personal_entries_do_not_touch_company_figures(self):
        self.add_txn(amount=Decimal("2500"))
        before = (income_expense_totals(2026, 10), account_balance(self.sbi), budget_vs_actual(2026, 10)["total_actual"])
        self.add_personal(amount=Decimal("5000"))
        self.add_personal(entry_type=Transaction.CREDIT, amount=Decimal("40000"), category=self.p_salary)
        after = (income_expense_totals(2026, 10), account_balance(self.sbi), budget_vs_actual(2026, 10)["total_actual"])
        self.assertEqual(before, after)

    def test_views_require_login(self):
        response = self.client.get(reverse("finance:personal_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_crud_views(self):
        self.login()
        self.client.get(reverse("finance:dashboard") + "?year=2026&month=10")
        response = self.client.post(
            reverse("finance:personal_transaction_create"),
            {
                "date": "2026-10-10",
                "entry_type": Transaction.DEBIT,
                "amount": "750",
                "category": self.p_food.pk,
                "payment_mode": PersonalTransaction.CASH,
                "description": "Groceries run",
            },
        )
        self.assertRedirects(response, reverse("finance:personal_transaction_list"))
        txn = PersonalTransaction.objects.get(description="Groceries run")

        bad = self.client.post(
            reverse("finance:personal_transaction_create"),
            {
                "date": "2026-10-10",
                "entry_type": Transaction.CREDIT,
                "amount": "750",
                "category": self.p_food.pk,
                "payment_mode": PersonalTransaction.CASH,
                "description": "Wrong",
            },
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(PersonalTransaction.objects.filter(description="Wrong").exists())

        dash = self.client.get(reverse("finance:personal_dashboard"))
        self.assertContains(dash, "₹750.00")
        listing = self.client.get(reverse("finance:personal_transaction_list"))
        self.assertContains(listing, "Groceries run")
        csv_response = self.client.get(reverse("finance:personal_transaction_list") + "?export=csv")
        self.assertIn(b"Groceries run", csv_response.content)

        self.client.post(
            reverse("finance:personal_transaction_edit", args=[txn.pk]),
            {
                "date": "2026-10-10",
                "entry_type": Transaction.DEBIT,
                "amount": "800",
                "category": self.p_food.pk,
                "payment_mode": PersonalTransaction.CARD,
                "description": "Groceries run",
            },
        )
        txn.refresh_from_db()
        self.assertEqual(txn.amount, Decimal("800.00"))

        self.client.post(reverse("finance:personal_category_delete", args=[self.p_food.pk]))
        self.p_food.refresh_from_db()
        self.assertFalse(self.p_food.is_active)

        self.client.post(reverse("finance:personal_transaction_delete", args=[txn.pk]))
        self.assertFalse(PersonalTransaction.objects.filter(pk=txn.pk).exists())

        self.client.post(reverse("finance:personal_category_delete", args=[self.p_medical.pk]))
        self.assertFalse(PersonalCategory.objects.filter(pk=self.p_medical.pk).exists())

        for name in ["personal_category_list", "personal_category_create", "personal_transaction_create"]:
            self.assertEqual(self.client.get(reverse(f"finance:{name}")).status_code, 200)
