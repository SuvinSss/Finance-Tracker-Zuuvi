from datetime import date
from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import (
    Account,
    Budget,
    Category,
    CompanySettings,
    Loan,
    PersonalCategory,
    PersonalDebt,
    PersonalDebtPayment,
    PersonalTransaction,
    Transaction,
)
from .period import MONTH_CHOICES


class StyledAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "form-control", "autofocus": True})
        self.fields["password"].widget.attrs.update({"class": "form-control"})
        self.error_messages["invalid_login"] = (
            "Please enter a valid username and password. Both fields may be case-sensitive."
        )


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "category_type", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "category_type": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_category_type(self):
        category_type = self.cleaned_data["category_type"]
        if (
            self.instance.pk
            and category_type != self.instance.category_type
            and self.instance.is_in_use()
        ):
            raise ValidationError(
                "This category is already used by transactions or budgets, so its type "
                "can't be changed. Create a new category instead."
            )
        return category_type


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = [
            "name",
            "account_type",
            "bank_name",
            "account_number",
            "opening_balance",
            "opening_balance_date",
            "notes",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "account_type": forms.Select(attrs={"class": "form-select"}),
            "bank_name": forms.TextInput(attrs={"class": "form-control"}),
            "account_number": forms.TextInput(attrs={"class": "form-control"}),
            "opening_balance": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "opening_balance_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        help_texts = {
            "account_number": "Optional. Last four digits are enough. Never store banking passwords, PINs, or OTPs.",
            "opening_balance_date": (
                "Transactions before this date are excluded from the current balance "
                "because they are assumed to already be part of the opening balance."
            ),
        }


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date",
            "entry_type",
            "amount",
            "category",
            "account",
            "description",
            "reference_number",
            "notes",
            "receipt",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "entry_type": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "account": forms.Select(attrs={"class": "form-select"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "reference_number": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "receipt": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by(
            "category_type", "name"
        )
        self.fields["account"].queryset = Account.objects.filter(is_active=True)
        if self.instance and self.instance.pk:
            if self.instance.category_id:
                self.fields["category"].queryset |= Category.objects.filter(pk=self.instance.category_id)
            if self.instance.account_id:
                self.fields["account"].queryset |= Account.objects.filter(pk=self.instance.account_id)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount

    def clean_receipt(self):
        receipt = self.cleaned_data.get("receipt")
        if receipt and hasattr(receipt, "size") and receipt.size > 5 * 1024 * 1024:
            raise ValidationError("Receipt files must be 5 MB or smaller.")
        return receipt

    def clean(self):
        cleaned = super().clean()
        entry_type = cleaned.get("entry_type")
        category = cleaned.get("category")
        if self.instance.loan_id and entry_type != Transaction.DEBIT:
            self.add_error("entry_type", "EMI payments must stay as debit transactions.")
        if category and entry_type == Transaction.DEBIT and category.category_type != Category.EXPENSE:
            self.add_error("category", "Debit transactions must use an expense category.")
        if category and entry_type == Transaction.CREDIT and category.category_type != Category.INCOME:
            self.add_error("category", "Credit transactions must use an income category.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.kind = Transaction.KIND_OPERATING
        instance.transfer_group = None
        if commit:
            instance.full_clean()
            instance.save()
        return instance


class TransferForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    source_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    destination_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    description = forms.CharField(
        max_length=255,
        initial="Internal transfer",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    reference_number = forms.CharField(
        required=False,
        max_length=80,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = Account.objects.filter(is_active=True)
        self.fields["source_account"].queryset = accounts
        self.fields["destination_account"].queryset = accounts

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source_account")
        dest = cleaned.get("destination_account")
        if source and dest and source.pk == dest.pk:
            raise ValidationError("Source and destination accounts must be different.")
        return cleaned


class LoanForm(forms.ModelForm):
    first_emi_month = forms.DateField(
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"class": "form-control", "type": "month"}),
        help_text=Loan._meta.get_field("first_emi_month").help_text,
    )

    class Meta:
        model = Loan
        fields = [
            "name",
            "lender",
            "loan_amount",
            "emi_amount",
            "emi_day",
            "first_emi_month",
            "tenure_months",
            "repayment_account",
            "notes",
            "is_active",
        ]
        labels = {"emi_amount": "EMI amount", "emi_day": "EMI due day", "tenure_months": "Number of EMIs"}
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "lender": forms.TextInput(attrs={"class": "form-control"}),
            "loan_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "emi_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "emi_day": forms.NumberInput(attrs={"class": "form-control", "min": "1", "max": "28"}),
            "tenure_months": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "repayment_account": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["repayment_account"].queryset = Account.objects.filter(is_active=True)
        if self.instance.pk and self.instance.repayment_account_id:
            self.fields["repayment_account"].queryset |= Account.objects.filter(
                pk=self.instance.repayment_account_id
            )

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.emi_payments.exists():
            first = cleaned.get("first_emi_month")
            tenure = cleaned.get("tenure_months")
            if first and tenure:
                trial = Loan(first_emi_month=first.replace(day=1), tenure_months=tenure)
                months = set(trial.schedule_months())
                outside = self.instance.emi_payments.exclude(emi_month__in=months)
                if outside.exists():
                    raise ValidationError(
                        "Some recorded EMI payments would fall outside the new schedule. "
                        "Adjust the first EMI month or number of EMIs."
                    )
        return cleaned


class EmiPaymentForm(forms.Form):
    emi_month = forms.TypedChoiceField(
        label="EMI month",
        coerce=lambda value: date.fromisoformat(value),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date = forms.DateField(
        label="Payment date",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
    )
    account = forms.ModelChoiceField(
        label="Paid from",
        queryset=Account.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference_number = forms.CharField(
        required=False,
        max_length=80,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def __init__(self, *args, loan, **kwargs):
        super().__init__(*args, **kwargs)
        self.loan = loan
        paid = set(loan.emi_payments.values_list("emi_month", flat=True))
        self.fields["emi_month"].choices = [
            (month.isoformat(), f"{month:%B %Y} (due {loan.due_date(month):%d %b})")
            for month in loan.schedule_months()
            if month not in paid
        ]
        self.fields["account"].queryset = Account.objects.filter(is_active=True)


class BudgetForm(forms.ModelForm):
    month = forms.TypedChoiceField(
        coerce=int,
        choices=MONTH_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Budget
        fields = ["year", "month", "category", "amount", "notes"]
        widgets = {
            "year": forms.NumberInput(attrs={"class": "form-control", "min": "2000"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            category_type=Category.EXPENSE, is_active=True
        )
        if self.instance and self.instance.pk and self.instance.category_id:
            self.fields["category"].queryset |= Category.objects.filter(pk=self.instance.category_id)

    def clean(self):
        cleaned = super().clean()
        year = cleaned.get("year")
        month = cleaned.get("month")
        category = cleaned.get("category")
        if year and month and category:
            qs = Budget.objects.filter(year=year, month=month, category=category)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("A budget for this category already exists for the selected month.")
        return cleaned


class PersonalCategoryForm(forms.ModelForm):
    class Meta:
        model = PersonalCategory
        fields = ["name", "category_type", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "category_type": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_category_type(self):
        category_type = self.cleaned_data["category_type"]
        if (
            self.instance.pk
            and category_type != self.instance.category_type
            and self.instance.is_in_use()
        ):
            raise ValidationError(
                "This category is already used by entries, so its type can't be changed. "
                "Create a new category instead."
            )
        return category_type


class PersonalTransactionForm(forms.ModelForm):
    class Meta:
        model = PersonalTransaction
        fields = ["date", "entry_type", "amount", "category", "payment_mode", "description", "notes"]
        labels = {"entry_type": "Type"}
        widgets = {
            "date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "entry_type": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "payment_mode": forms.Select(attrs={"class": "form-select"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = PersonalCategory.objects.filter(is_active=True).order_by(
            "category_type", "name"
        )
        if self.instance and self.instance.pk and self.instance.category_id:
            self.fields["category"].queryset |= PersonalCategory.objects.filter(pk=self.instance.category_id)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        entry_type = cleaned.get("entry_type")
        category = cleaned.get("category")
        if category and entry_type == Transaction.DEBIT and category.category_type != Category.EXPENSE:
            self.add_error("category", "Expenses must use an expense category.")
        if category and entry_type == Transaction.CREDIT and category.category_type != Category.INCOME:
            self.add_error("category", "Income must use an income category.")
        return cleaned


EMI_FIELDS = ["installment_amount", "installment_day", "first_installment_month", "tenure_months"]


class PersonalDebtForm(forms.ModelForm):
    emi_fields = EMI_FIELDS
    first_installment_month = forms.DateField(
        required=False,
        label="First installment month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"class": "form-control", "type": "month"}),
    )

    class Meta:
        model = PersonalDebt
        fields = [
            "counterparty",
            "counterparty_type",
            "amount",
            "start_date",
            "payment_mode",
            "due_date",
            "has_emi_plan",
            "installment_amount",
            "installment_day",
            "first_installment_month",
            "tenure_months",
            "notes",
        ]
        labels = {
            "counterparty_type": "Lender / borrower type",
            "start_date": "Date",
            "payment_mode": "Given / received via",
            "installment_amount": "Installment amount",
            "installment_day": "Installment due day",
            "tenure_months": "Number of installments",
        }
        widgets = {
            "counterparty": forms.TextInput(attrs={"class": "form-control"}),
            "counterparty_type": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "payment_mode": forms.Select(attrs={"class": "form-select"}),
            "due_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "has_emi_plan": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "installment_amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "installment_day": forms.NumberInput(attrs={"class": "form-control", "min": "1", "max": "28"}),
            "tenure_months": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, direction, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.direction = direction
        lent = direction == PersonalDebt.LENT
        self.fields["counterparty"].label = "Borrower (person or bank)" if lent else "Lender (person or bank)"
        self.fields["counterparty_type"].label = "Borrower type" if lent else "Lender type"
        self.fields["payment_mode"].label = "Given via" if lent else "Received via"
        self.fields["amount"].help_text = "Amount you gave." if lent else "Amount you received."
        self.fields["has_emi_plan"].label = (
            "They repay in monthly installments (EMI)" if lent else "I repay in monthly installments (EMI)"
        )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("has_emi_plan"):
            for name in EMI_FIELDS:
                if cleaned.get(name) in (None, ""):
                    self.add_error(name, "Required for an installment plan.")
        return cleaned


class PersonalDebtPaymentForm(forms.ModelForm):
    class Meta:
        model = PersonalDebtPayment
        fields = ["date", "amount", "payment_mode", "notes"]
        labels = {"payment_mode": "Via"}
        widgets = {
            "date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "payment_mode": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount


class SettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = ["company_name", "currency", "financial_year_start_month", "budget_warning_threshold"]
        widgets = {
            "company_name": forms.TextInput(attrs={"class": "form-control"}),
            "currency": forms.TextInput(attrs={"class": "form-control"}),
            "financial_year_start_month": forms.Select(
                choices=MONTH_CHOICES, attrs={"class": "form-select"}
            ),
            "budget_warning_threshold": forms.NumberInput(
                attrs={"class": "form-control", "min": "1", "max": "100"}
            ),
        }
