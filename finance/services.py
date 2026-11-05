from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    Account,
    Budget,
    Category,
    CompanySettings,
    CreditCard,
    CreditCardEntry,
    Loan,
    PersonalDebt,
    PersonalTransaction,
    Transaction,
)
from .money import ZERO, as_money
from .period import month_bounds, month_name


STATUS_WITHIN = "Within Budget"
STATUS_NEAR = "Near Limit"
STATUS_FULL = "Fully Used"
STATUS_OVER = "Overspent"
STATUS_UNBUDGETED = "No Budget"


def _sum(qs, **filters) -> Decimal:
    total = qs.filter(**filters).aggregate(total=Sum("amount"))["total"]
    return as_money(total)


def operating_queryset():
    return Transaction.objects.filter(kind=Transaction.KIND_OPERATING).select_related(
        "category", "account"
    )


def account_balance(account: Account, as_of: date | None = None) -> Decimal:
    """
    Current Balance = Opening Balance + credits on/after opening date - debits on/after opening date.

    Transactions dated before opening_balance_date are excluded. They are treated as already
    included in the opening balance.
    """
    qs = Transaction.objects.filter(account=account, date__gte=account.opening_balance_date)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    credits = _sum(qs, entry_type=Transaction.CREDIT)
    debits = _sum(qs, entry_type=Transaction.DEBIT)
    return as_money(account.opening_balance + credits - debits)


def account_movement(account: Account) -> dict:
    qs = Transaction.objects.filter(account=account, date__gte=account.opening_balance_date)
    credits = _sum(qs, entry_type=Transaction.CREDIT)
    debits = _sum(qs, entry_type=Transaction.DEBIT)
    return {
        "opening_balance": as_money(account.opening_balance),
        "total_credits": credits,
        "total_debits": debits,
        "current_balance": as_money(account.opening_balance + credits - debits),
    }


def total_account_balance() -> Decimal:
    total = ZERO
    for account in Account.objects.filter(is_active=True):
        total += account_balance(account)
    return as_money(total)


def filter_by_period(qs, year: int | None = None, month: int | None = None, start=None, end=None):
    if start and end:
        return qs.filter(date__gte=start, date__lte=end)
    if year and month:
        start, end = month_bounds(year, month)
        return qs.filter(date__gte=start, date__lte=end)
    if year:
        return qs.filter(date__year=year)
    return qs


def income_expense_totals(year=None, month=None, start=None, end=None) -> dict:
    qs = filter_by_period(operating_queryset(), year, month, start, end)
    income = _sum(qs, entry_type=Transaction.CREDIT, category__category_type=Category.INCOME)
    expenses = _sum(qs, entry_type=Transaction.DEBIT, category__category_type=Category.EXPENSE)
    return {
        "income": income,
        "expenses": expenses,
        "net_cash_flow": as_money(income - expenses),
    }


def expenses_by_category(year=None, month=None, start=None, end=None) -> list[dict]:
    qs = filter_by_period(operating_queryset(), year, month, start, end).filter(
        entry_type=Transaction.DEBIT,
        category__category_type=Category.EXPENSE,
    )
    rows = (
        qs.values("category_id", "category__name")
        .annotate(actual=Sum("amount"))
        .order_by("category__name")
    )
    return [
        {
            "category_id": row["category_id"],
            "category": row["category__name"],
            "actual": as_money(row["actual"]),
        }
        for row in rows
    ]


def utilization_status(budget: Decimal, actual: Decimal, threshold: int) -> tuple[Decimal, Decimal, str]:
    remaining = as_money(budget - actual)
    if budget <= 0:
        return remaining, ZERO, STATUS_UNBUDGETED
    utilization = as_money((actual / budget) * Decimal("100"))
    if actual > budget:
        status = STATUS_OVER
    elif actual == budget:
        status = STATUS_FULL
    elif utilization >= Decimal(threshold):
        status = STATUS_NEAR
    else:
        status = STATUS_WITHIN
    return remaining, utilization, status


def budget_vs_actual(year: int, month: int, threshold: int | None = None) -> dict:
    if threshold is None:
        threshold = CompanySettings.get_solo().budget_warning_threshold

    budgets = list(
        Budget.objects.filter(year=year, month=month).select_related("category")
    )
    actual_map = {
        row["category_id"]: as_money(row["actual"])
        for row in expenses_by_category(year, month)
        if row["category_id"]
    }

    rows = []
    total_budget = ZERO
    total_actual_budgeted = ZERO
    for budget in budgets:
        actual = actual_map.pop(budget.category_id, ZERO)
        remaining, utilization, status = utilization_status(budget.amount, actual, threshold)
        total_budget += as_money(budget.amount)
        total_actual_budgeted += actual
        rows.append(
            {
                "budget_id": budget.id,
                "category_id": budget.category_id,
                "category": budget.category.name,
                "budget": as_money(budget.amount),
                "actual": actual,
                "remaining": remaining,
                "utilization": utilization,
                "status": status,
                "notes": budget.notes,
                "has_budget": True,
            }
        )

    unbudgeted = []
    unbudgeted_actual = ZERO
    for category_id, actual in actual_map.items():
        category = Category.objects.filter(pk=category_id).first()
        unbudgeted_actual += actual
        unbudgeted.append(
            {
                "budget_id": None,
                "category_id": category_id,
                "category": category.name if category else "Unknown",
                "budget": ZERO,
                "actual": actual,
                "remaining": as_money(-actual),
                "utilization": None,
                "status": STATUS_UNBUDGETED,
                "notes": "",
                "has_budget": False,
            }
        )

    remaining_budget = as_money(total_budget - total_actual_budgeted - unbudgeted_actual)
    warnings = [
        row
        for row in rows
        if row["status"] in {STATUS_NEAR, STATUS_FULL, STATUS_OVER}
    ]
    return {
        "rows": rows,
        "unbudgeted": unbudgeted,
        "total_budget": total_budget,
        "total_actual": as_money(total_actual_budgeted + unbudgeted_actual),
        "budgeted_actual": total_actual_budgeted,
        "unbudgeted_actual": unbudgeted_actual,
        "remaining_budget": remaining_budget,
        "warnings": warnings,
        "threshold": threshold,
    }


def dashboard_summary(year: int, month: int) -> dict:
    ie = income_expense_totals(year, month)
    bva = budget_vs_actual(year, month)
    return {
        "year": year,
        "month": month,
        "month_label": f"{month_name(month)} {year}",
        "total_expense_budget": bva["total_budget"],
        "actual_expenses": ie["expenses"],
        "remaining_expense_budget": bva["remaining_budget"],
        "total_income": ie["income"],
        "total_expenses": ie["expenses"],
        "net_cash_flow": ie["net_cash_flow"],
        "total_account_balance": total_account_balance(),
        "budget_vs_actual": bva,
    }


def monthly_summaries(year: int) -> list[dict]:
    rows = []
    for month in range(1, 13):
        ie = income_expense_totals(year, month)
        bva = budget_vs_actual(year, month)
        rows.append(
            {
                "month": month,
                "month_name": month_name(month),
                "income": ie["income"],
                "expenses": ie["expenses"],
                "net_cash_flow": ie["net_cash_flow"],
                "total_budget": bva["total_budget"],
                "remaining_budget": bva["remaining_budget"],
            }
        )
    return rows


def next_month_run_estimate(year: int, month: int) -> dict:
    """Estimate cash needed next month using this month's expense budget, falling back to actuals."""
    bva = budget_vs_actual(year, month)
    ie = income_expense_totals(year, month)
    needed = bva["total_budget"] if bva["total_budget"] > 0 else ie["expenses"]
    cash = total_account_balance()
    return {
        "needed": needed,
        "cash_on_hand": cash,
        "shortfall": as_money(needed - cash) if cash < needed else ZERO,
    }


@transaction.atomic
def create_transfer(*, date, amount, source: Account, destination: Account, description, reference="", notes=""):
    if source.pk == destination.pk:
        raise ValueError("Source and destination accounts must be different.")
    amount = as_money(amount)
    if amount <= 0:
        raise ValueError("Transfer amount must be greater than zero.")
    group = uuid4()
    debit = Transaction(
        date=date,
        entry_type=Transaction.DEBIT,
        amount=amount,
        category=None,
        account=source,
        description=description,
        reference_number=reference,
        notes=notes,
        kind=Transaction.KIND_TRANSFER,
        transfer_group=group,
    )
    credit = Transaction(
        date=date,
        entry_type=Transaction.CREDIT,
        amount=amount,
        category=None,
        account=destination,
        description=description,
        reference_number=reference,
        notes=notes,
        kind=Transaction.KIND_TRANSFER,
        transfer_group=group,
    )
    debit.full_clean()
    credit.full_clean()
    debit.save()
    credit.save()
    return group, debit, credit


@transaction.atomic
def update_transfer(group, *, date, amount, source: Account, destination: Account, description, reference="", notes=""):
    legs = list(Transaction.objects.select_for_update().filter(transfer_group=group, kind=Transaction.KIND_TRANSFER))
    if len(legs) != 2:
        raise ValueError("Transfer record is incomplete and cannot be updated.")
    Transaction.objects.filter(transfer_group=group).delete()
    return create_transfer(
        date=date,
        amount=amount,
        source=source,
        destination=destination,
        description=description,
        reference=reference,
        notes=notes,
    )


@transaction.atomic
def delete_transfer(group):
    deleted, _ = Transaction.objects.filter(
        transfer_group=group, kind=Transaction.KIND_TRANSFER
    ).delete()
    return deleted


def transfer_legs(group):
    legs = list(
        Transaction.objects.filter(transfer_group=group, kind=Transaction.KIND_TRANSFER).select_related("account")
    )
    debit = next((leg for leg in legs if leg.entry_type == Transaction.DEBIT), None)
    credit = next((leg for leg in legs if leg.entry_type == Transaction.CREDIT), None)
    return debit, credit


EMI_CATEGORY_NAME = "Loan EMI"

EMI_PAID = "Paid"
EMI_DUE = "Due"
EMI_OVERDUE = "Overdue"
EMI_UPCOMING = "Upcoming"


def ensure_emi_category() -> Category:
    category, _ = Category.objects.get_or_create(
        name=EMI_CATEGORY_NAME,
        category_type=Category.EXPENSE,
        defaults={"description": "Loan EMI payments (full EMI counted as expense)"},
    )
    return category


def emi_state(loan: Loan, emi_month: date, paid: bool, today: date) -> str:
    if paid:
        return EMI_PAID
    due = loan.due_date(emi_month)
    if due < today:
        return EMI_OVERDUE
    if (due.year, due.month) == (today.year, today.month):
        return EMI_DUE
    return EMI_UPCOMING


def loan_schedule(loan: Loan, today: date | None = None) -> list[dict]:
    today = today or timezone.localdate()
    payments = {txn.emi_month: txn for txn in loan.emi_payments.all()}
    return [
        {
            "emi_month": emi_month,
            "due_date": loan.due_date(emi_month),
            "payment": payments.get(emi_month),
            "state": emi_state(loan, emi_month, emi_month in payments, today),
        }
        for emi_month in loan.schedule_months()
    ]


def loan_status(loan: Loan, today: date | None = None) -> dict:
    """
    Remaining Payable = Remaining EMIs x EMI amount.
    Paid count comes from linked EMI payment transactions, so deleting a payment reopens that month.
    """
    schedule = loan_schedule(loan, today)
    paid = [row for row in schedule if row["state"] == EMI_PAID]
    unpaid = [row for row in schedule if row["state"] != EMI_PAID]
    remaining = len(unpaid)
    return {
        "loan": loan,
        "schedule": schedule,
        "paid_count": len(paid),
        "total_paid": as_money(sum((row["payment"].amount for row in paid), ZERO)),
        "remaining_emis": remaining,
        "remaining_payable": as_money(loan.emi_amount * remaining),
        "next_due": unpaid[0] if unpaid else None,
        "overdue": [row for row in schedule if row["state"] == EMI_OVERDUE],
    }


def emi_reminders(year: int, month: int, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    selected = date(year, month, 1)
    due_this_month = []
    overdue = []
    outstanding = ZERO
    for loan in Loan.objects.filter(is_active=True).prefetch_related("emi_payments"):
        status = loan_status(loan, today)
        outstanding += status["remaining_payable"]
        for row in status["schedule"]:
            if row["emi_month"] == selected:
                due_this_month.append({"loan": loan, **row})
        overdue.extend({"loan": loan, **row} for row in status["overdue"])
    return {
        "due_this_month": due_this_month,
        "overdue": overdue,
        "total_outstanding": as_money(outstanding),
        "has_loans": Loan.objects.filter(is_active=True).exists(),
    }


@transaction.atomic
def record_emi_payment(*, loan: Loan, emi_month: date, date, amount, account: Account, reference="", notes=""):
    txn = Transaction(
        date=date,
        entry_type=Transaction.DEBIT,
        amount=as_money(amount),
        category=ensure_emi_category(),
        account=account,
        description=f"EMI {emi_month:%b %Y} – {loan.name}",
        reference_number=reference,
        notes=notes,
        kind=Transaction.KIND_OPERATING,
        loan=loan,
        emi_month=emi_month,
    )
    txn.full_clean()
    txn.save()
    return txn


# --- Personal (owner) finances: separate from company figures -----------------


def personal_totals(year=None, month=None, start=None, end=None) -> dict:
    qs = filter_by_period(PersonalTransaction.objects.all(), year, month, start, end)
    income = _sum(qs, entry_type=Transaction.CREDIT)
    expenses = _sum(qs, entry_type=Transaction.DEBIT)
    return {"income": income, "expenses": expenses, "net": as_money(income - expenses)}


def personal_by_category(year=None, month=None, entry_type=Transaction.DEBIT, start=None, end=None) -> list[dict]:
    qs = filter_by_period(PersonalTransaction.objects.all(), year, month, start, end).filter(
        entry_type=entry_type
    )
    rows = list(
        qs.values("category_id", "category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total", "category__name")
    )
    grand = sum((as_money(row["total"]) for row in rows), ZERO)
    return [
        {
            "category_id": row["category_id"],
            "category": row["category__name"],
            "total": as_money(row["total"]),
            "share": (as_money(row["total"]) * 100 / grand).quantize(Decimal("0.1")) if grand else ZERO,
        }
        for row in rows
    ]


def personal_monthly_trend(year: int) -> list[dict]:
    rows = []
    for month in range(1, 13):
        totals = personal_totals(year, month)
        rows.append({"month": month, "month_name": month_name(month), **totals})
    return rows


# --- Lend & borrow (owner's personal debts): separate from income/expense -----

DEBT_SETTLED = "Settled"
DEBT_OPEN = "Open"
INSTALLMENT_PARTLY_PAID = "Partly paid"


def _date_state(due: date, today: date) -> str:
    if due < today:
        return EMI_OVERDUE
    if (due.year, due.month) == (today.year, today.month):
        return EMI_DUE
    return EMI_UPCOMING


def debt_schedule(debt: PersonalDebt, total_paid: Decimal, today: date) -> list[dict]:
    """
    Installment rows with payments allocated oldest-first, so a lump sum covers several months
    and a short payment shows the installment as partly paid.
    """
    rows = []
    remaining = total_paid
    for number, month in enumerate(debt.schedule_months(), start=1):
        due = debt.due_date_for(month)
        expected = debt.installment_amount
        covered = min(remaining, expected) if remaining > 0 else ZERO
        remaining -= covered
        balance = as_money(expected - covered)
        date_state = _date_state(due, today)
        if balance <= 0:
            state = EMI_PAID
        elif covered > 0:
            state = INSTALLMENT_PARTLY_PAID
        else:
            state = date_state
        rows.append(
            {
                "number": number,
                "month": month,
                "due_date": due,
                "expected": as_money(expected),
                "covered": as_money(covered),
                "balance": balance,
                "state": state,
                "overdue": balance > 0 and date_state == EMI_OVERDUE,
                "due_now": balance > 0 and date_state == EMI_DUE,
            }
        )
    return rows


def debt_status(debt: PersonalDebt, today: date | None = None) -> dict:
    """Outstanding = total due − payments recorded. Deleting a payment reopens the balance."""
    today = today or timezone.localdate()
    payments = list(debt.payments.all())
    total_paid = as_money(sum((p.amount for p in payments), ZERO))
    total_due = as_money(debt.total_due)
    outstanding = max(as_money(total_due - total_paid), ZERO)
    settled = outstanding <= 0

    schedule = debt_schedule(debt, total_paid, today) if debt.has_emi_plan else []
    unpaid = [row for row in schedule if row["balance"] > 0]
    overdue_rows = [row for row in schedule if row["overdue"]]
    overdue_amount = as_money(sum((row["balance"] for row in overdue_rows), ZERO))

    if settled:
        state = DEBT_SETTLED
    elif debt.has_emi_plan:
        if overdue_rows:
            state = EMI_OVERDUE
        elif any(row["due_now"] for row in schedule):
            state = EMI_DUE
        else:
            state = DEBT_OPEN
    elif debt.due_date:
        state = _date_state(debt.due_date, today)
        if state == EMI_UPCOMING:
            state = DEBT_OPEN
        if state == EMI_OVERDUE:
            overdue_amount = outstanding
    else:
        state = DEBT_OPEN

    if settled:
        next_due_date, next_amount = None, ZERO
    elif unpaid:
        next_due_date, next_amount = unpaid[0]["due_date"], unpaid[0]["balance"]
    else:
        next_due_date, next_amount = debt.due_date, outstanding

    return {
        "debt": debt,
        "payments": payments,
        "total_due": total_due,
        "total_paid": total_paid,
        "outstanding": outstanding,
        "settled": settled,
        "state": state,
        "schedule": schedule,
        "paid_installments": sum(1 for row in schedule if row["state"] == EMI_PAID),
        "overdue_rows": overdue_rows,
        "overdue_amount": overdue_amount,
        "next_due_date": next_due_date,
        "next_amount": as_money(next_amount),
    }


def debt_summary(today: date | None = None, statuses: list[dict] | None = None, direction=None) -> dict:
    today = today or timezone.localdate()
    if statuses is None:
        statuses = [debt_status(d, today) for d in PersonalDebt.objects.prefetch_related("payments")]
    if direction:
        statuses = [s for s in statuses if s["debt"].direction == direction]
    receivable = payable = ZERO
    attention = []
    for status in statuses:
        if status["settled"]:
            continue
        if status["debt"].is_lent:
            receivable += status["outstanding"]
        else:
            payable += status["outstanding"]
        if status["state"] in {EMI_OVERDUE, EMI_DUE}:
            attention.append(status)
    attention.sort(key=lambda s: (s["state"] != EMI_OVERDUE, s["next_due_date"] or today))
    return {
        "receivable": as_money(receivable),
        "payable": as_money(payable),
        "net": as_money(receivable - payable),
        "attention": attention,
        "overdue_count": sum(1 for s in attention if s["state"] == EMI_OVERDUE),
        "open_count": sum(1 for s in statuses if not s["settled"]),
        "total_paid": as_money(sum((s["total_paid"] for s in statuses), ZERO)),
    }


# --- Credit cards (personal): spends are personal expenses; payments/refunds only reduce the card balance

CARD_NO_DUES = "No dues"
LIMIT_OK = "Within limit"
LIMIT_NEAR = "Near limit"
LIMIT_OVER = "Over limit"


def _card_spends(card: CreditCard):
    return card.spends.filter(date__gte=card.opening_balance_date)


def _card_entries(card: CreditCard):
    return card.entries.filter(date__gte=card.opening_balance_date)


def card_balance(card: CreditCard, as_of: date | None = None) -> Decimal:
    """Outstanding = opening outstanding + spends − bill payments − refunds, up to `as_of` (inclusive)."""
    spends, entries = _card_spends(card), _card_entries(card)
    if as_of is not None:
        if as_of < card.opening_balance_date:
            return ZERO
        spends, entries = spends.filter(date__lte=as_of), entries.filter(date__lte=as_of)
    return as_money(card.opening_balance + _sum(spends) - _sum(entries))


def card_status(card: CreditCard, today: date | None = None, threshold: int | None = None) -> dict:
    today = today or timezone.localdate()
    if threshold is None:
        threshold = CompanySettings.get_solo().budget_warning_threshold

    outstanding = card_balance(card)
    available = as_money(card.credit_limit - outstanding)
    _remaining, utilization, budget_state = utilization_status(card.credit_limit, max(outstanding, ZERO), threshold)
    if budget_state == STATUS_OVER:
        limit_state = LIMIT_OVER
    elif budget_state in {STATUS_NEAR, STATUS_FULL}:
        limit_state = LIMIT_NEAR
    else:
        limit_state = LIMIT_OK

    last_statement = card.last_statement_date(today)
    next_statement = card.next_statement_date(last_statement)
    has_statement = last_statement >= card.opening_balance_date
    if has_statement:
        statement_balance = card_balance(card, last_statement)
        credits_since = _sum(_card_entries(card).filter(date__gt=last_statement, date__lte=today))
        amount_due = max(as_money(statement_balance - credits_since), ZERO)
        due_date = card.due_date_for(last_statement)
        if amount_due > 0:
            due_state = EMI_OVERDUE if due_date < today else EMI_DUE
        elif statement_balance > 0:
            due_state = EMI_PAID
        else:
            due_state = CARD_NO_DUES
    else:
        statement_balance = amount_due = ZERO
        due_date = None
        due_state = CARD_NO_DUES

    unbilled = _sum(_card_spends(card).filter(date__gt=last_statement, date__lte=today))
    return {
        "card": card,
        "outstanding": outstanding,
        "available": available,
        "utilization": utilization,
        "utilization_bar": min(utilization, Decimal("100")),
        "limit_state": limit_state,
        "last_statement": last_statement if has_statement else None,
        "next_statement": next_statement,
        "statement_balance": statement_balance,
        "amount_due": amount_due,
        "due_date": due_date,
        "due_state": due_state,
        "unbilled": unbilled,
    }


def card_summary(today: date | None = None, statuses: list[dict] | None = None) -> dict:
    today = today or timezone.localdate()
    if statuses is None:
        threshold = CompanySettings.get_solo().budget_warning_threshold
        statuses = [card_status(c, today, threshold) for c in CreditCard.objects.filter(is_active=True)]
    attention = [s for s in statuses if s["due_state"] in {EMI_DUE, EMI_OVERDUE}]
    attention.sort(key=lambda s: (s["due_state"] != EMI_OVERDUE, s["due_date"]))
    total_limit = as_money(sum((s["card"].credit_limit for s in statuses), ZERO))
    total_outstanding = as_money(sum((s["outstanding"] for s in statuses), ZERO))
    return {
        "count": len(statuses),
        "total_limit": total_limit,
        "total_outstanding": total_outstanding,
        "total_available": as_money(total_limit - total_outstanding),
        "total_due": as_money(sum((s["amount_due"] for s in attention), ZERO)),
        "attention": attention,
        "overdue_count": sum(1 for s in attention if s["due_state"] == EMI_OVERDUE),
    }


def card_ledger(card: CreditCard) -> list[dict]:
    """Spends, payments and refunds since the opening date, oldest first, with the running outstanding."""
    rows = []
    for spend in _card_spends(card).select_related("category"):
        rows.append(
            {
                "date": spend.date,
                "kind": "spend",
                "label": "Spend",
                "description": spend.description,
                "category": spend.category.name,
                "amount": spend.amount,
                "signed": spend.amount,
                "spend": spend,
                "entry": None,
                "sort": (spend.date, spend.created_at),
            }
        )
    for entry in _card_entries(card):
        rows.append(
            {
                "date": entry.date,
                "kind": entry.kind,
                "label": entry.get_kind_display(),
                "description": entry.description or entry.get_kind_display(),
                "category": "",
                "amount": entry.amount,
                "signed": -entry.amount,
                "spend": None,
                "entry": entry,
                "sort": (entry.date, entry.created_at),
            }
        )
    rows.sort(key=lambda r: r["sort"])
    balance = as_money(card.opening_balance)
    for row in rows:
        balance = as_money(balance + row["signed"])
        row["balance"] = balance
    return rows


def card_statements(card: CreditCard, today: date | None = None, count: int = 6) -> list[dict]:
    """Recent billing cycles (newest first): spends, credits and the closing bill amount."""
    today = today or timezone.localdate()
    statements = []
    end = card.last_statement_date(today)
    for _ in range(count):
        if end < card.opening_balance_date:
            break
        start = card.last_statement_date(end - timedelta(days=1))
        spends = _card_spends(card).filter(date__gt=start, date__lte=end)
        credits = _card_entries(card).filter(date__gt=start, date__lte=end)
        statements.append(
            {
                "start": max(start + timedelta(days=1), card.opening_balance_date),
                "end": end,
                "due_date": card.due_date_for(end),
                "spends": _sum(spends),
                "credits": _sum(credits),
                "closing": card_balance(card, end),
            }
        )
        end = start
    return statements


def year_choices():
    current = timezone.localdate().year
    return list(range(current - 5, current + 3))
