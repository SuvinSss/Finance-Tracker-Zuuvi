import calendar
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q


def month_sequence(first: date, count: int) -> list[date]:
    """First day of `count` consecutive months starting at `first`."""
    months = []
    year, month = first.year, first.month
    for _ in range(count):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


class CompanySettings(models.Model):
    company_name = models.CharField(max_length=200, default="Zuuvi")
    currency = models.CharField(max_length=8, default="INR")
    financial_year_start_month = models.PositiveSmallIntegerField(
        default=4,
        help_text="Calendar month when the financial year starts (1–12). Default is April.",
    )
    budget_warning_threshold = models.PositiveSmallIntegerField(
        default=90,
        help_text="Utilization percentage at which a category is marked Near Limit.",
    )

    class Meta:
        verbose_name = "Company settings"
        verbose_name_plural = "Company settings"

    def __str__(self):
        return self.company_name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get_solo(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        if not 1 <= self.financial_year_start_month <= 12:
            raise ValidationError({"financial_year_start_month": "Must be between 1 and 12."})
        if not 1 <= self.budget_warning_threshold <= 100:
            raise ValidationError({"budget_warning_threshold": "Must be between 1 and 100."})


class Category(models.Model):
    INCOME = "income"
    EXPENSE = "expense"
    TYPE_CHOICES = [
        (INCOME, "Income"),
        (EXPENSE, "Expense"),
    ]

    name = models.CharField(max_length=100)
    category_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "category_type"],
                name="unique_category_name_type",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.get_category_type_display()})"

    def has_transactions(self):
        return self.transactions.exists()

    def is_in_use(self):
        return self.has_transactions() or self.budgets.exists()


class Account(models.Model):
    BANK = "bank"
    CASH = "cash"
    PETTY_CASH = "petty_cash"
    UPI = "upi"
    TYPE_CHOICES = [
        (BANK, "Bank"),
        (CASH, "Cash"),
        (PETTY_CASH, "Petty Cash"),
        (UPI, "UPI"),
    ]

    name = models.CharField(max_length=100, unique=True)
    account_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    bank_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(
        max_length=32,
        blank=True,
        help_text="Full number or last four digits. Do not store PINs, passwords, or OTPs.",
    )
    opening_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    opening_balance_date = models.DateField(
        help_text=(
            "Transactions dated before this date are excluded from the current balance "
            "because they are assumed to already be reflected in the opening balance."
        )
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.account_type in {self.CASH, self.PETTY_CASH}:
            return
        # Bank/UPI may omit bank_name; no extra required fields.

    def has_transactions(self):
        return self.transactions.exists()


class Loan(models.Model):
    name = models.CharField(max_length=100, unique=True, help_text="For example: Delivery van loan")
    lender = models.CharField(max_length=100, help_text="Bank or lender name")
    loan_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Original loan amount, for reference only.",
    )
    emi_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    emi_day = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="Day of the month the EMI is due (1–28).",
    )
    first_emi_month = models.DateField(
        help_text=(
            "Month of the first EMI you will record here. If the loan started earlier, "
            "enter the next EMI month and the number of EMIs left."
        )
    )
    tenure_months = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Number of EMIs from the first EMI month.",
    )
    repayment_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="loans",
        help_text="Account the EMI is usually paid from.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(emi_amount__gt=0), name="loan_emi_positive"),
            models.CheckConstraint(condition=Q(emi_day__gte=1, emi_day__lte=28), name="loan_emi_day_valid"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.first_emi_month:
            self.first_emi_month = self.first_emi_month.replace(day=1)
        super().save(*args, **kwargs)

    def schedule_months(self):
        """First day of each EMI month, in order."""
        return month_sequence(self.first_emi_month, self.tenure_months)

    def due_date(self, emi_month):
        return emi_month.replace(day=self.emi_day)


class Transaction(models.Model):
    DEBIT = "debit"
    CREDIT = "credit"
    ENTRY_CHOICES = [
        (DEBIT, "Debit (money out)"),
        (CREDIT, "Credit (money in)"),
    ]

    KIND_OPERATING = "operating"
    KIND_TRANSFER = "transfer"
    KIND_CHOICES = [
        (KIND_OPERATING, "Operating"),
        (KIND_TRANSFER, "Internal transfer"),
    ]

    date = models.DateField()
    entry_type = models.CharField(max_length=10, choices=ENTRY_CHOICES)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    description = models.CharField(max_length=255)
    reference_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    receipt = models.FileField(
        upload_to="receipts/%Y/%m/",
        blank=True,
        validators=[FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "webp"])],
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_OPERATING)
    transfer_group = models.UUIDField(null=True, blank=True, db_index=True)
    loan = models.ForeignKey(
        Loan,
        on_delete=models.PROTECT,
        related_name="emi_payments",
        null=True,
        blank=True,
    )
    emi_month = models.DateField(null=True, blank=True, help_text="EMI month this payment covers.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="txn_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(kind="operating", category__isnull=False)
                    | Q(kind="transfer", category__isnull=True)
                ),
                name="txn_category_matches_kind",
            ),
            models.CheckConstraint(
                condition=Q(loan__isnull=True, emi_month__isnull=True)
                | Q(loan__isnull=False, emi_month__isnull=False, kind="operating", entry_type="debit"),
                name="txn_emi_fields_together",
            ),
            models.UniqueConstraint(
                fields=["loan", "emi_month"],
                condition=Q(loan__isnull=False),
                name="unique_emi_payment_per_month",
                violation_error_message="This EMI month has already been paid.",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.entry_type} {self.amount} {self.description}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})

        if self.kind == self.KIND_TRANSFER:
            if self.category_id:
                raise ValidationError(
                    {"category": "Internal transfers must not have an income or expense category."}
                )
            return

        if not self.category_id:
            raise ValidationError({"category": "Category is required for income and expense transactions."})

        category = self.category
        if category is None and self.category_id:
            category = Category.objects.filter(pk=self.category_id).first()
        if category is None:
            raise ValidationError({"category": "Select a valid category."})

        if self.entry_type == self.DEBIT and category.category_type != Category.EXPENSE:
            raise ValidationError(
                {"category": "Debit (expense) transactions must use an expense category."}
            )
        if self.entry_type == self.CREDIT and category.category_type != Category.INCOME:
            raise ValidationError(
                {"category": "Credit (income) transactions must use an income category."}
            )


class PersonalCategory(models.Model):
    name = models.CharField(max_length=100)
    category_type = models.CharField(max_length=10, choices=Category.TYPE_CHOICES)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category_type", "name"]
        verbose_name_plural = "Personal categories"
        constraints = [
            models.UniqueConstraint(
                fields=["name", "category_type"],
                name="unique_personal_category_name_type",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.get_category_type_display()})"

    def is_in_use(self):
        return self.transactions.exists()


def _day_in_month(year: int, month: int, day: int) -> date:
    """`day` of the given month, clamped to the month's last day (e.g. 31 → 28/29 in February)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


class CreditCard(models.Model):
    """Owner's personal credit card. Spends are PersonalTransactions; payments/refunds are CreditCardEntries."""

    name = models.CharField(max_length=100, unique=True, help_text="For example: HDFC Millennia")
    issuer = models.CharField(max_length=100, blank=True, verbose_name="Bank / issuer")
    last_four = models.CharField(
        max_length=4,
        blank=True,
        verbose_name="Last 4 digits",
        validators=[RegexValidator(r"^\d{4}$", "Enter exactly 4 digits.")],
        help_text="Optional. Never store the full card number, CVV, or PIN.",
    )
    credit_limit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    bill_day = models.PositiveSmallIntegerField(
        verbose_name="Bill (statement) day",
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Day of the month the bill is generated (1–31; 31 means the last day of the month).",
    )
    due_day = models.PositiveSmallIntegerField(
        verbose_name="Payment due day",
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Day of the month the bill must be paid. If it's on or before the bill day, it's in the following month.",
    )
    opening_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name="Opening outstanding",
        help_text="Amount already owed on the card on the opening date. Use 0 for a new or fully paid card.",
    )
    opening_balance_date = models.DateField(
        verbose_name="Opening date",
        help_text=(
            "Card transactions start from this date. Tip: use your last bill date and that bill's "
            "outstanding so its due amount is tracked."
        ),
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(credit_limit__gt=0), name="credit_card_limit_positive"),
            models.CheckConstraint(condition=Q(bill_day__gte=1, bill_day__lte=31), name="credit_card_bill_day_valid"),
            models.CheckConstraint(condition=Q(due_day__gte=1, due_day__lte=31), name="credit_card_due_day_valid"),
            models.CheckConstraint(condition=Q(opening_balance__gte=0), name="credit_card_opening_non_negative"),
        ]

    def __str__(self):
        return f"{self.name} ••{self.last_four}" if self.last_four else self.name

    def statement_date(self, year: int, month: int) -> date:
        return _day_in_month(year, month, self.bill_day)

    def due_date_for(self, statement: date) -> date:
        if self.due_day > self.bill_day:
            return _day_in_month(statement.year, statement.month, self.due_day)
        year, month = _add_months(statement.year, statement.month, 1)
        return _day_in_month(year, month, self.due_day)

    def last_statement_date(self, today: date) -> date:
        """Most recent bill date on or before `today`."""
        current = self.statement_date(today.year, today.month)
        if current <= today:
            return current
        return self.statement_date(*_add_months(today.year, today.month, -1))

    def next_statement_date(self, after: date) -> date:
        return self.statement_date(*_add_months(after.year, after.month, 1))

    def is_in_use(self):
        return self.spends.exists() or self.entries.exists()


class PersonalTransaction(models.Model):
    """Owner's personal income/expense entry. Kept apart from company accounts, budgets and reports."""

    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK = "bank"
    PAYMENT_MODE_CHOICES = [
        (CASH, "Cash"),
        (UPI, "UPI"),
        (CARD, "Card"),
        (BANK, "Bank transfer"),
    ]

    date = models.DateField()
    entry_type = models.CharField(
        max_length=10,
        choices=[(Transaction.DEBIT, "Expense (money out)"), (Transaction.CREDIT, "Income (money in)")],
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    category = models.ForeignKey(
        PersonalCategory,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    payment_mode = models.CharField(max_length=10, choices=PAYMENT_MODE_CHOICES, default=UPI)
    credit_card = models.ForeignKey(
        CreditCard,
        on_delete=models.PROTECT,
        related_name="spends",
        null=True,
        blank=True,
        help_text="Only for expenses paid by credit card. The spend counts against this card's limit.",
    )
    description = models.CharField(max_length=255)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="personal_txn_amount_positive"),
            models.CheckConstraint(
                condition=Q(credit_card__isnull=True) | Q(entry_type="debit", payment_mode="card"),
                name="personal_txn_credit_card_is_card_expense",
                violation_error_message="Only card expenses can be linked to a credit card.",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.entry_type} {self.amount} {self.description}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})
        if not self.category_id:
            return
        category = self.category
        if self.entry_type == Transaction.DEBIT and category.category_type != Category.EXPENSE:
            raise ValidationError({"category": "Expenses must use an expense category."})
        if self.entry_type == Transaction.CREDIT and category.category_type != Category.INCOME:
            raise ValidationError({"category": "Income must use an income category."})


class CreditCardEntry(models.Model):
    """A bill payment or refund/cashback on a credit card. Frees up limit; not counted as income or expense."""

    PAYMENT = "payment"
    REFUND = "refund"
    KIND_CHOICES = [
        (PAYMENT, "Bill payment"),
        (REFUND, "Refund / cashback"),
    ]

    card = models.ForeignKey(CreditCard, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=PAYMENT)
    date = models.DateField()
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    description = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name_plural = "Credit card entries"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="credit_card_entry_amount_positive"),
        ]

    def __str__(self):
        return f"{self.date} {self.get_kind_display()} {self.amount} ({self.card.name})"


class PersonalDebt(models.Model):
    """Money the owner lent to someone, or borrowed from a person or bank. Kept apart from income/expense."""

    LENT = "lent"
    BORROWED = "borrowed"
    DIRECTION_CHOICES = [
        (LENT, "I lent money (they owe me)"),
        (BORROWED, "I borrowed money (I owe them)"),
    ]

    PERSON = "person"
    BANK = "bank"
    COUNTERPARTY_TYPE_CHOICES = [
        (PERSON, "Person"),
        (BANK, "Bank / institution"),
    ]

    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    counterparty = models.CharField(max_length=100, verbose_name="Person or bank")
    counterparty_type = models.CharField(max_length=10, choices=COUNTERPARTY_TYPE_CHOICES, default=PERSON)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Amount given or taken.",
    )
    start_date = models.DateField(help_text="Date the money was given or received.")
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional. When the full amount should be returned (for one-time repayment).",
    )
    payment_mode = models.CharField(
        max_length=10,
        choices=PersonalTransaction.PAYMENT_MODE_CHOICES,
        default=PersonalTransaction.UPI,
    )
    has_emi_plan = models.BooleanField(
        default=False,
        verbose_name="Repaid in monthly installments (EMI)",
    )
    installment_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    installment_day = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="Day of the month each installment is due (1–28).",
    )
    first_installment_month = models.DateField(null=True, blank=True)
    tenure_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Number of monthly installments.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="personal_debt_amount_positive"),
            models.CheckConstraint(
                condition=Q(has_emi_plan=False)
                | Q(
                    installment_amount__gt=0,
                    installment_day__gte=1,
                    installment_day__lte=28,
                    first_installment_month__isnull=False,
                    tenure_months__gte=1,
                ),
                name="personal_debt_emi_fields",
            ),
        ]

    def __str__(self):
        verb = "Lent to" if self.direction == self.LENT else "Borrowed from"
        return f"{verb} {self.counterparty} – {self.amount} on {self.start_date}"

    @property
    def is_lent(self):
        return self.direction == self.LENT

    def save(self, *args, **kwargs):
        if self.first_installment_month:
            self.first_installment_month = self.first_installment_month.replace(day=1)
        if not self.has_emi_plan:
            self.installment_amount = None
            self.installment_day = None
            self.first_installment_month = None
            self.tenure_months = None
        super().save(*args, **kwargs)

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})
        if self.due_date and self.start_date and self.due_date < self.start_date:
            raise ValidationError({"due_date": "Due date can't be before the start date."})
        if self.has_emi_plan:
            errors = {}
            for field in ["installment_amount", "installment_day", "first_installment_month", "tenure_months"]:
                if getattr(self, field) in (None, ""):
                    errors[field] = "Required for an installment plan."
            if errors:
                raise ValidationError(errors)

    @property
    def total_due(self) -> Decimal:
        if self.has_emi_plan and self.installment_amount and self.tenure_months:
            return self.installment_amount * self.tenure_months
        return self.amount

    def schedule_months(self):
        if not self.has_emi_plan or not self.first_installment_month or not self.tenure_months:
            return []
        return month_sequence(self.first_installment_month, self.tenure_months)

    def due_date_for(self, month: date) -> date:
        return month.replace(day=self.installment_day)


class PersonalDebtPayment(models.Model):
    """Money received back (for a lent record) or paid back (for a borrowed record)."""

    debt = models.ForeignKey(PersonalDebt, on_delete=models.CASCADE, related_name="payments")
    date = models.DateField()
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_mode = models.CharField(
        max_length=10,
        choices=PersonalTransaction.PAYMENT_MODE_CHOICES,
        default=PersonalTransaction.UPI,
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="personal_debt_payment_amount_positive"),
        ]

    def __str__(self):
        return f"{self.date} {self.amount} ({self.debt.counterparty})"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})


class Budget(models.Model):
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="budgets",
        limit_choices_to={"category_type": Category.EXPENSE},
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["category__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["year", "month", "category"],
                name="unique_budget_year_month_category",
            ),
            models.CheckConstraint(condition=Q(month__gte=1, month__lte=12), name="budget_month_valid"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="budget_amount_positive"),
        ]

    def __str__(self):
        return f"{self.year}-{self.month:02d} {self.category} {self.amount}"

    def clean(self):
        if self.month and not 1 <= self.month <= 12:
            raise ValidationError({"month": "Month must be between 1 and 12."})
        if self.category_id:
            category = self.category
            if category.category_type != Category.EXPENSE:
                raise ValidationError({"category": "Budgets can only be created for expense categories."})
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Budget amount must be greater than zero."})
