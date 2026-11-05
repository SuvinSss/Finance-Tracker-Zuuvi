import csv
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AccountForm,
    BudgetForm,
    CategoryForm,
    CreditCardEntryForm,
    CreditCardForm,
    CreditCardTransactionForm,
    EmiPaymentForm,
    LoanForm,
    PersonalCategoryForm,
    PersonalDebtForm,
    PersonalDebtPaymentForm,
    PersonalTransactionForm,
    SettingsForm,
    StyledAuthenticationForm,
    TransactionForm,
    TransferForm,
)
from .models import (
    Account,
    Budget,
    Category,
    CompanySettings,
    CreditCard,
    CreditCardEntry,
    Loan,
    PersonalCategory,
    PersonalDebt,
    PersonalDebtPayment,
    PersonalTransaction,
    Transaction,
)
from .money import ZERO, as_money
from .period import get_selected_period, month_name
from .services import (
    account_movement,
    budget_vs_actual,
    card_ledger,
    card_statements,
    card_status,
    card_summary,
    create_transfer,
    dashboard_summary,
    debt_status,
    debt_summary,
    delete_transfer,
    emi_reminders,
    expenses_by_category,
    income_expense_totals,
    loan_status,
    monthly_summaries,
    next_month_run_estimate,
    personal_by_category,
    personal_monthly_trend,
    personal_totals,
    record_emi_payment,
    total_account_balance,
    transfer_legs,
    update_transfer,
)


class OwnerLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = StyledAuthenticationForm
    redirect_authenticated_user = True


class OwnerLogoutView(LogoutView):
    next_page = reverse_lazy("login")


def _csv_response(filename: str, headers: list[str], rows: list[list]):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(headers)
    writer.writerows(rows)
    return response


@login_required
def dashboard(request):
    year, month = get_selected_period(request)
    summary = dashboard_summary(year, month)
    bva = summary["budget_vs_actual"]
    accounts = [
        {"account": account, **account_movement(account)}
        for account in Account.objects.filter(is_active=True)
    ]
    recent = (
        Transaction.objects.select_related("category", "account")
        .order_by("-date", "-id")[:8]
    )
    estimate = next_month_run_estimate(year, month)
    chart = {
        "income": str(summary["total_income"]),
        "expenses": str(summary["total_expenses"]),
    }
    return render(
        request,
        "finance/dashboard.html",
        {
            "summary": summary,
            "bva": bva,
            "accounts": accounts,
            "recent": recent,
            "estimate": estimate,
            "chart": chart,
            "emi": emi_reminders(year, month),
        },
    )


@login_required
def transaction_list(request):
    qs = Transaction.objects.select_related("category", "account")
    q = request.GET.get("q", "").strip()
    entry_type = request.GET.get("entry_type", "")
    category_id = request.GET.get("category", "")
    account_id = request.GET.get("account", "")
    start = request.GET.get("start", "")
    end = request.GET.get("end", "")
    sort = request.GET.get("sort", "-date")
    year, month = get_selected_period(request)

    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    if not start and not end and request.GET.get("all") != "1":
        qs = qs.filter(date__year=year, date__month=month)

    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(reference_number__icontains=q) | Q(notes__icontains=q))
    if entry_type in {Transaction.DEBIT, Transaction.CREDIT}:
        qs = qs.filter(entry_type=entry_type)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if account_id:
        qs = qs.filter(account_id=account_id)

    allowed_sorts = {
        "date": "date",
        "-date": "-date",
        "amount": "amount",
        "-amount": "-amount",
        "description": "description",
        "-description": "-description",
    }
    qs = qs.order_by(allowed_sorts.get(sort, "-date"), "-id")

    paginator = Paginator(qs, 20)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "finance/transaction_list.html",
        {
            "page": page,
            "q": q,
            "entry_type": entry_type,
            "category_id": category_id,
            "account_id": account_id,
            "start": start,
            "end": end,
            "sort": sort,
            "categories": Category.objects.filter(is_active=True),
            "accounts": Account.objects.filter(is_active=True),
            "showing_all": request.GET.get("all") == "1",
        },
    )


@login_required
def transaction_create(request):
    initial = {"date": timezone.localdate()}
    form = TransactionForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Transaction saved.")
        return redirect("finance:transaction_list")
    return render(request, "finance/transaction_form.html", {"form": form, "title": "Add transaction"})


@login_required
def transaction_edit(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    if txn.kind == Transaction.KIND_TRANSFER:
        return redirect("finance:transfer_edit", group=txn.transfer_group)
    form = TransactionForm(request.POST or None, request.FILES or None, instance=txn)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Transaction updated. Budget and account figures have been recalculated.")
        return redirect("finance:transaction_list")
    return render(request, "finance/transaction_form.html", {"form": form, "title": "Edit transaction", "txn": txn})


@login_required
def transaction_detail(request, pk):
    txn = get_object_or_404(Transaction.objects.select_related("category", "account"), pk=pk)
    return render(request, "finance/transaction_detail.html", {"txn": txn})


@login_required
def transaction_delete(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    if txn.kind == Transaction.KIND_TRANSFER:
        return redirect("finance:transfer_delete", group=txn.transfer_group)
    if request.method == "POST":
        txn.delete()
        messages.success(request, "Transaction deleted. Totals have been recalculated.")
        return redirect("finance:transaction_list")
    return render(request, "finance/confirm_delete.html", {"object": txn, "object_type": "transaction"})


@login_required
def transfer_create(request):
    initial = {"date": timezone.localdate()}
    form = TransferForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        create_transfer(
            date=form.cleaned_data["date"],
            amount=form.cleaned_data["amount"],
            source=form.cleaned_data["source_account"],
            destination=form.cleaned_data["destination_account"],
            description=form.cleaned_data["description"],
            reference=form.cleaned_data["reference_number"],
            notes=form.cleaned_data["notes"],
        )
        messages.success(request, "Internal transfer recorded. It will not affect income, expenses, or budgets.")
        return redirect("finance:transaction_list")
    return render(request, "finance/transfer_form.html", {"form": form, "title": "Transfer between accounts"})


@login_required
def transfer_edit(request, group):
    debit, credit = transfer_legs(group)
    if not debit or not credit:
        raise Http404("Transfer not found.")
    initial = {
        "date": debit.date,
        "amount": debit.amount,
        "source_account": debit.account_id,
        "destination_account": credit.account_id,
        "description": debit.description,
        "reference_number": debit.reference_number,
        "notes": debit.notes,
    }
    form = TransferForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        update_transfer(
            group,
            date=form.cleaned_data["date"],
            amount=form.cleaned_data["amount"],
            source=form.cleaned_data["source_account"],
            destination=form.cleaned_data["destination_account"],
            description=form.cleaned_data["description"],
            reference=form.cleaned_data["reference_number"],
            notes=form.cleaned_data["notes"],
        )
        messages.success(request, "Transfer updated.")
        return redirect("finance:transaction_list")
    return render(request, "finance/transfer_form.html", {"form": form, "title": "Edit transfer"})


@login_required
def transfer_delete(request, group):
    debit, credit = transfer_legs(group)
    if not debit or not credit:
        raise Http404("Transfer not found.")
    if request.method == "POST":
        delete_transfer(group)
        messages.success(request, "Transfer deleted. Account balances have been recalculated.")
        return redirect("finance:transaction_list")
    return render(
        request,
        "finance/confirm_delete.html",
        {"object": debit, "object_type": "internal transfer"},
    )


@login_required
def budget_list(request):
    year, month = get_selected_period(request)
    category_id = request.GET.get("category", "")
    year_filter = request.GET.get("filter_year")
    if year_filter:
        year = int(year_filter)
        request.session["period_year"] = year
    bva = budget_vs_actual(year, month)
    budgets = Budget.objects.filter(year=year, month=month).select_related("category")
    if category_id:
        budgets = budgets.filter(category_id=category_id)
        bva["rows"] = [row for row in bva["rows"] if str(row["category_id"]) == str(category_id)]
        bva["unbudgeted"] = [row for row in bva["unbudgeted"] if str(row["category_id"]) == str(category_id)]
    return render(
        request,
        "finance/budget_list.html",
        {
            "bva": bva,
            "budgets": budgets,
            "category_id": category_id,
            "expense_categories": Category.objects.filter(category_type=Category.EXPENSE),
        },
    )


@login_required
def budget_create(request):
    year, month = get_selected_period(request)
    form = BudgetForm(request.POST or None, initial={"year": year, "month": month})
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Budget saved.")
        return redirect("finance:budget_list")
    return render(request, "finance/budget_form.html", {"form": form, "title": "Add budget"})


@login_required
def budget_edit(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    form = BudgetForm(request.POST or None, instance=budget)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Budget updated.")
        return redirect("finance:budget_list")
    return render(request, "finance/budget_form.html", {"form": form, "title": "Edit budget"})


@login_required
def budget_delete(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    if request.method == "POST":
        budget.delete()
        messages.success(request, "Budget deleted.")
        return redirect("finance:budget_list")
    return render(request, "finance/confirm_delete.html", {"object": budget, "object_type": "budget"})


@login_required
def category_list(request):
    return render(
        request,
        "finance/category_list.html",
        {"categories": Category.objects.all()},
    )


@login_required
def category_create(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Category created.")
        return redirect("finance:category_list")
    return render(request, "finance/category_form.html", {"form": form, "title": "Add category"})


@login_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Category updated.")
        return redirect("finance:category_list")
    return render(request, "finance/category_form.html", {"form": form, "title": "Edit category"})


@login_required
@require_POST
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if category.is_in_use():
        category.is_active = False
        category.save(update_fields=["is_active"])
        messages.warning(
            request,
            "This category is used by existing transactions or budgets, so it was deactivated instead of deleted.",
        )
        return redirect("finance:category_list")
    category.delete()
    messages.success(request, "Category deleted.")
    return redirect("finance:category_list")


@login_required
def account_list(request):
    accounts = [
        {"account": account, **account_movement(account)}
        for account in Account.objects.all()
    ]
    return render(
        request,
        "finance/account_list.html",
        {"accounts": accounts, "total_balance": total_account_balance()},
    )


@login_required
def account_create(request):
    form = AccountForm(request.POST or None, initial={"opening_balance_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Account created.")
        return redirect("finance:account_list")
    return render(request, "finance/account_form.html", {"form": form, "title": "Add account"})


@login_required
def account_edit(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = AccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Account updated.")
        return redirect("finance:account_detail", pk=account.pk)
    return render(request, "finance/account_form.html", {"form": form, "title": "Edit account"})


@login_required
def account_detail(request, pk):
    account = get_object_or_404(Account, pk=pk)
    movement = account_movement(account)
    txns = account.transactions.select_related("category").order_by("-date", "-id")[:25]
    return render(
        request,
        "finance/account_detail.html",
        {"account": account, "movement": movement, "txns": txns},
    )


@login_required
@require_POST
def account_delete(request, pk):
    account = get_object_or_404(Account, pk=pk)
    if account.has_transactions() or account.loans.exists():
        account.is_active = False
        account.save(update_fields=["is_active"])
        messages.warning(
            request,
            "This account has transactions, so it was deactivated instead of deleted.",
        )
        return redirect("finance:account_list")
    account.delete()
    messages.success(request, "Account deleted.")
    return redirect("finance:account_list")


@login_required
def loan_list(request):
    loans = [loan_status(loan) for loan in Loan.objects.select_related("repayment_account").prefetch_related("emi_payments")]
    return render(
        request,
        "finance/loan_list.html",
        {
            "loans": loans,
            "total_outstanding": sum(
                (item["remaining_payable"] for item in loans if item["loan"].is_active), ZERO
            ),
        },
    )


@login_required
def loan_create(request):
    form = LoanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        loan = form.save()
        messages.success(request, "Loan added.")
        return redirect("finance:loan_detail", pk=loan.pk)
    return render(request, "finance/loan_form.html", {"form": form, "title": "Add loan"})


@login_required
def loan_edit(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    form = LoanForm(request.POST or None, instance=loan)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Loan updated.")
        return redirect("finance:loan_detail", pk=loan.pk)
    return render(request, "finance/loan_form.html", {"form": form, "title": "Edit loan"})


@login_required
def loan_detail(request, pk):
    loan = get_object_or_404(Loan.objects.select_related("repayment_account"), pk=pk)
    return render(request, "finance/loan_detail.html", {"status": loan_status(loan), "loan": loan})


@login_required
def emi_pay(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    status = loan_status(loan)
    if status["next_due"] is None:
        messages.info(request, "All EMIs for this loan are already paid.")
        return redirect("finance:loan_detail", pk=loan.pk)
    month = request.GET.get("month", "")
    emi_month = status["next_due"]["emi_month"]
    if month:
        try:
            emi_month = date.fromisoformat(f"{month}-01")
        except ValueError:
            pass
    initial = {
        "emi_month": emi_month.isoformat(),
        "date": timezone.localdate(),
        "amount": loan.emi_amount,
        "account": loan.repayment_account_id,
    }
    form = EmiPaymentForm(request.POST or None, loan=loan, initial=initial)
    if request.method == "POST" and form.is_valid():
        record_emi_payment(
            loan=loan,
            emi_month=form.cleaned_data["emi_month"],
            date=form.cleaned_data["date"],
            amount=form.cleaned_data["amount"],
            account=form.cleaned_data["account"],
            reference=form.cleaned_data["reference_number"],
            notes=form.cleaned_data["notes"],
        )
        messages.success(request, "EMI payment recorded. It counts as a Loan EMI expense.")
        return redirect("finance:loan_detail", pk=loan.pk)
    return render(request, "finance/emi_payment_form.html", {"form": form, "loan": loan})


@login_required
@require_POST
def loan_delete(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    if loan.emi_payments.exists():
        loan.is_active = False
        loan.save(update_fields=["is_active"])
        messages.warning(request, "This loan has EMI payments, so it was closed instead of deleted.")
        return redirect("finance:loan_list")
    loan.delete()
    messages.success(request, "Loan deleted.")
    return redirect("finance:loan_list")


@login_required
def reports(request):
    year, month = get_selected_period(request)
    start = request.GET.get("start") or None
    end = request.GET.get("end") or None
    report = request.GET.get("report", "budget")

    if start and end:
        ie = income_expense_totals(start=start, end=end)
        by_cat = expenses_by_category(start=start, end=end)
        period_label = f"{start} to {end}"
        bva = budget_vs_actual(year, month)
    else:
        ie = income_expense_totals(year, month)
        by_cat = expenses_by_category(year, month)
        bva = budget_vs_actual(year, month)
        period_label = f"{month_name(month)} {year}"

    summaries = monthly_summaries(year)

    if request.GET.get("export") == "csv":
        if report == "income":
            return _csv_response(
                f"income-expense-{period_label}.csv",
                ["Period", "Income", "Expenses", "Net cash flow"],
                [[period_label, ie["income"], ie["expenses"], ie["net_cash_flow"]]],
            )
        if report == "expense_category":
            return _csv_response(
                f"expense-by-category-{period_label}.csv",
                ["Category", "Actual expenses"],
                [[row["category"], row["actual"]] for row in by_cat],
            )
        if report == "monthly":
            return _csv_response(
                f"monthly-summary-{year}.csv",
                ["Month", "Income", "Expenses", "Net cash flow", "Total budget", "Remaining budget"],
                [
                    [
                        row["month_name"],
                        row["income"],
                        row["expenses"],
                        row["net_cash_flow"],
                        row["total_budget"],
                        row["remaining_budget"],
                    ]
                    for row in summaries
                ],
            )
        return _csv_response(
            f"budget-vs-actual-{year}-{month:02d}.csv",
            ["Category", "Budget", "Actual", "Remaining", "Utilization %", "Status"],
            [
                [
                    row["category"],
                    row["budget"],
                    row["actual"],
                    row["remaining"],
                    row["utilization"] if row["utilization"] is not None else "",
                    row["status"],
                ]
                for row in bva["rows"] + bva["unbudgeted"]
            ],
        )

    return render(
        request,
        "finance/reports.html",
        {
            "ie": ie,
            "bva": bva,
            "by_cat": by_cat,
            "summaries": summaries,
            "period_label": period_label,
            "start": start or "",
            "end": end or "",
            "report": report,
        },
    )


@login_required
def settings_view(request):
    settings = CompanySettings.get_solo()
    form = SettingsForm(request.POST or None, instance=settings)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("finance:settings")
    return render(request, "finance/settings.html", {"form": form})


@login_required
def personal_dashboard(request):
    year, month = get_selected_period(request)
    trend = personal_monthly_trend(year)
    by_cat = personal_by_category(year, month)
    return render(
        request,
        "finance/personal/dashboard.html",
        {
            "month_label": f"{month_name(month)} {year}",
            "year": year,
            "totals": personal_totals(year, month),
            "year_totals": personal_totals(year),
            "by_cat": by_cat,
            "income_by_cat": personal_by_category(year, month, entry_type=Transaction.CREDIT),
            "recent": PersonalTransaction.objects.select_related("category", "credit_card")[:10],
            "debts": debt_summary(),
            "cards": card_summary(),
            "chart": {
                "cat_labels": [row["category"] for row in by_cat],
                "cat_values": [str(row["total"]) for row in by_cat],
                "months": [row["month_name"][:3] for row in trend],
                "income": [str(row["income"]) for row in trend],
                "expenses": [str(row["expenses"]) for row in trend],
            },
        },
    )


@login_required
def personal_transaction_list(request):
    qs = PersonalTransaction.objects.select_related("category", "credit_card")
    q = request.GET.get("q", "").strip()
    card_id = request.GET.get("card", "")
    entry_type = request.GET.get("entry_type", "")
    category_id = request.GET.get("category", "")
    payment_mode = request.GET.get("payment_mode", "")
    start = request.GET.get("start", "")
    end = request.GET.get("end", "")
    sort = request.GET.get("sort", "-date")
    year, month = get_selected_period(request)

    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    if not start and not end and request.GET.get("all") != "1":
        qs = qs.filter(date__year=year, date__month=month)

    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(notes__icontains=q))
    if entry_type in {Transaction.DEBIT, Transaction.CREDIT}:
        qs = qs.filter(entry_type=entry_type)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if payment_mode:
        qs = qs.filter(payment_mode=payment_mode)
    if card_id:
        qs = qs.filter(credit_card_id=card_id)

    allowed_sorts = {"date", "-date", "amount", "-amount", "description", "-description"}
    qs = qs.order_by(sort if sort in allowed_sorts else "-date", "-id")

    if request.GET.get("export") == "csv":
        return _csv_response(
            "personal-transactions.csv",
            ["Date", "Type", "Category", "Payment mode", "Credit card", "Description", "Amount", "Notes"],
            [
                [
                    txn.date,
                    "Income" if txn.entry_type == Transaction.CREDIT else "Expense",
                    txn.category.name,
                    txn.get_payment_mode_display(),
                    txn.credit_card.name if txn.credit_card else "",
                    txn.description,
                    txn.amount,
                    txn.notes,
                ]
                for txn in qs
            ],
        )

    totals = qs.aggregate(
        income=Sum("amount", filter=Q(entry_type=Transaction.CREDIT)),
        expenses=Sum("amount", filter=Q(entry_type=Transaction.DEBIT)),
    )
    paginator = Paginator(qs, 20)
    page = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("export", None)
    return render(
        request,
        "finance/personal/transaction_list.html",
        {
            "page": page,
            "q": q,
            "entry_type": entry_type,
            "category_id": category_id,
            "payment_mode": payment_mode,
            "start": start,
            "end": end,
            "sort": sort,
            "categories": PersonalCategory.objects.filter(is_active=True),
            "payment_modes": PersonalTransaction.PAYMENT_MODE_CHOICES,
            "cards": CreditCard.objects.all(),
            "card_id": card_id,
            "showing_all": request.GET.get("all") == "1",
            "filtered_income": as_money(totals["income"]),
            "filtered_expenses": as_money(totals["expenses"]),
            "querystring": params.urlencode(),
        },
    )


@login_required
def personal_transaction_create(request):
    initial = {"date": timezone.localdate(), "entry_type": Transaction.DEBIT}
    form = PersonalTransactionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Personal entry saved.")
        if "save_add" in request.POST:
            return redirect("finance:personal_transaction_create")
        return redirect("finance:personal_transaction_list")
    return render(request, "finance/personal/transaction_form.html", {"form": form, "title": "Add personal entry"})


def _safe_next(request, default):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return default


@login_required
def personal_transaction_edit(request, pk):
    txn = get_object_or_404(PersonalTransaction, pk=pk)
    form = PersonalTransactionForm(request.POST or None, instance=txn)
    next_url = _safe_next(request, reverse("finance:personal_transaction_list"))
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Personal entry updated.")
        return redirect(next_url)
    return render(
        request,
        "finance/personal/transaction_form.html",
        {"form": form, "title": "Edit personal entry", "txn": txn, "next_url": next_url},
    )


@login_required
def personal_transaction_delete(request, pk):
    txn = get_object_or_404(PersonalTransaction, pk=pk)
    next_url = _safe_next(request, reverse("finance:personal_transaction_list"))
    if request.method == "POST":
        txn.delete()
        messages.success(request, "Personal entry deleted.")
        return redirect(next_url)
    return render(
        request,
        "finance/confirm_delete.html",
        {"object": txn, "object_type": "personal entry", "next_url": next_url},
    )


@login_required
def personal_category_list(request):
    return render(
        request,
        "finance/personal/category_list.html",
        {"categories": PersonalCategory.objects.all()},
    )


@login_required
def personal_category_create(request):
    form = PersonalCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Personal category created.")
        return redirect("finance:personal_category_list")
    return render(request, "finance/personal/category_form.html", {"form": form, "title": "Add personal category"})


@login_required
def personal_category_edit(request, pk):
    category = get_object_or_404(PersonalCategory, pk=pk)
    form = PersonalCategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Personal category updated.")
        return redirect("finance:personal_category_list")
    return render(request, "finance/personal/category_form.html", {"form": form, "title": "Edit personal category"})


@login_required
@require_POST
def personal_category_delete(request, pk):
    category = get_object_or_404(PersonalCategory, pk=pk)
    if category.is_in_use():
        category.is_active = False
        category.save(update_fields=["is_active"])
        messages.warning(request, "This category has entries, so it was deactivated instead of deleted.")
        return redirect("finance:personal_category_list")
    category.delete()
    messages.success(request, "Personal category deleted.")
    return redirect("finance:personal_category_list")


# --- Lend & borrow (separate "Money Lent" and "Money Borrowed" sections) -------


def _debt_list_url(direction):
    return f"finance:personal_{direction}_list"


@login_required
def personal_debt_list(request, direction):
    status_filter = request.GET.get("status", "open")
    q = request.GET.get("q", "").strip()

    all_statuses = [
        debt_status(d) for d in PersonalDebt.objects.filter(direction=direction).prefetch_related("payments")
    ]
    summary = debt_summary(statuses=all_statuses, direction=direction)

    rows = all_statuses
    if status_filter == "open":
        rows = [r for r in rows if not r["settled"]]
    elif status_filter == "settled":
        rows = [r for r in rows if r["settled"]]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in r["debt"].counterparty.lower() or needle in r["debt"].notes.lower()]

    is_lent = direction == PersonalDebt.LENT
    if request.GET.get("export") == "csv":
        return _csv_response(
            "money-lent.csv" if is_lent else "money-borrowed.csv",
            [
                "Date", "Borrower" if is_lent else "Lender", "Amount", "Total due",
                "Received back" if is_lent else "Paid back", "Outstanding",
                "Installments", "Next due", "Status", "Notes",
            ],
            [
                [
                    r["debt"].start_date,
                    r["debt"].counterparty,
                    r["debt"].amount,
                    r["total_due"],
                    r["total_paid"],
                    r["outstanding"],
                    f"{r['debt'].installment_amount} x {r['debt'].tenure_months}" if r["debt"].has_emi_plan else "",
                    r["next_due_date"] or "",
                    r["state"],
                    r["debt"].notes,
                ]
                for r in rows
            ],
        )

    params = request.GET.copy()
    params.pop("export", None)
    return render(
        request,
        "finance/personal/debt_list.html",
        {
            "rows": rows,
            "summary": summary,
            "is_lent": is_lent,
            "debt_direction": direction,
            "status_filter": status_filter,
            "q": q,
            "querystring": params.urlencode(),
        },
    )


@login_required
def personal_debt_create(request, direction):
    form = PersonalDebtForm(request.POST or None, direction=direction, initial={"start_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        debt = form.save()
        messages.success(request, "Record saved.")
        return redirect("finance:personal_debt_detail", pk=debt.pk)
    title = "Add money lent" if direction == PersonalDebt.LENT else "Add money borrowed"
    return render(
        request,
        "finance/personal/debt_form.html",
        {"form": form, "title": title, "debt_direction": direction},
    )


@login_required
def personal_debt_edit(request, pk):
    debt = get_object_or_404(PersonalDebt, pk=pk)
    form = PersonalDebtForm(request.POST or None, instance=debt, direction=debt.direction)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Record updated.")
        return redirect("finance:personal_debt_detail", pk=debt.pk)
    title = "Edit money lent" if debt.is_lent else "Edit money borrowed"
    return render(
        request,
        "finance/personal/debt_form.html",
        {"form": form, "title": title, "debt": debt, "debt_direction": debt.direction},
    )


@login_required
def personal_debt_detail(request, pk):
    debt = get_object_or_404(PersonalDebt.objects.prefetch_related("payments"), pk=pk)
    return render(
        request,
        "finance/personal/debt_detail.html",
        {"debt": debt, "status": debt_status(debt), "debt_direction": debt.direction},
    )


@login_required
def personal_debt_delete(request, pk):
    debt = get_object_or_404(PersonalDebt, pk=pk)
    if request.method == "POST":
        direction = debt.direction
        debt.delete()
        messages.success(request, "Record and its repayments deleted.")
        return redirect(_debt_list_url(direction))
    return render(
        request,
        "finance/confirm_delete.html",
        {
            "object": debt,
            "object_type": "money lent record" if debt.is_lent else "money borrowed record",
            "debt_direction": debt.direction,
        },
    )


@login_required
def personal_debt_payment_create(request, pk):
    debt = get_object_or_404(PersonalDebt.objects.prefetch_related("payments"), pk=pk)
    status = debt_status(debt)
    initial = {
        "date": timezone.localdate(),
        "amount": status["next_amount"] or None,
        "payment_mode": debt.payment_mode,
    }
    form = PersonalDebtPaymentForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.debt = debt
        payment.save()
        if payment.amount > status["outstanding"]:
            messages.warning(
                request,
                f"This payment is more than the {status['outstanding']} that was outstanding. "
                "The record is now settled; check the amount if that's unexpected.",
            )
        else:
            messages.success(request, "Money received recorded." if debt.is_lent else "Repayment recorded.")
        return redirect("finance:personal_debt_detail", pk=debt.pk)
    return render(
        request,
        "finance/personal/debt_payment_form.html",
        {"form": form, "debt": debt, "status": status, "debt_direction": debt.direction},
    )


@login_required
@require_POST
def personal_debt_payment_delete(request, pk):
    payment = get_object_or_404(PersonalDebtPayment, pk=pk)
    debt_pk = payment.debt_id
    payment.delete()
    messages.success(request, "Payment removed. The outstanding balance has been updated.")
    return redirect("finance:personal_debt_detail", pk=debt_pk)


# --- Credit cards (personal) ---------------------------------------------------


@login_required
def personal_card_list(request):
    statuses = [card_status(card) for card in CreditCard.objects.all()]
    active = [s for s in statuses if s["card"].is_active]
    return render(
        request,
        "finance/personal/card_list.html",
        {"statuses": statuses, "summary": card_summary(statuses=active)},
    )


@login_required
def personal_card_create(request):
    form = CreditCardForm(request.POST or None, initial={"opening_balance_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        card = form.save()
        messages.success(request, "Credit card added.")
        return redirect("finance:personal_card_detail", pk=card.pk)
    return render(request, "finance/personal/card_form.html", {"form": form, "title": "Add credit card"})


@login_required
def personal_card_edit(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    form = CreditCardForm(request.POST or None, instance=card)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Credit card updated.")
        return redirect("finance:personal_card_detail", pk=card.pk)
    return render(
        request,
        "finance/personal/card_form.html",
        {"form": form, "title": f"Edit {card.name}", "card": card},
    )


@login_required
def personal_card_detail(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    ledger = card_ledger(card)
    if request.GET.get("export") == "csv":
        return _csv_response(
            f"credit-card-{card.pk}.csv",
            ["Date", "Type", "Description", "Category", "Amount", "Outstanding after"],
            [
                [row["date"], row["label"], row["description"], row["category"], row["signed"], row["balance"]]
                for row in ledger
            ],
        )
    return render(
        request,
        "finance/personal/card_detail.html",
        {
            "card": card,
            "status": card_status(card),
            "statements": card_statements(card),
            "ledger": list(reversed(ledger)),
            "detail_url": reverse("finance:personal_card_detail", args=[card.pk]),
        },
    )


@login_required
def personal_card_delete(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    if card.is_in_use():
        messages.error(
            request,
            "This card has transactions, so it can't be deleted. Edit it and untick Active to hide it instead.",
        )
        return redirect("finance:personal_card_detail", pk=card.pk)
    if request.method == "POST":
        card.delete()
        messages.success(request, "Credit card deleted.")
        return redirect("finance:personal_card_list")
    return render(request, "finance/confirm_delete.html", {"object": card, "object_type": "credit card"})


@login_required
def personal_card_txn_create(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    kind = request.GET.get("kind", CreditCardTransactionForm.SPEND)
    initial = {"date": timezone.localdate(), "kind": kind}
    if kind == CreditCardEntry.PAYMENT:
        status = card_status(card)
        initial["amount"] = status["amount_due"] or status["outstanding"] or None
        initial["description"] = "Bill payment"
    form = CreditCardTransactionForm(request.POST or None, card=card, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{dict(form.KIND_CHOICES)[form.cleaned_data['kind']]} recorded.")
        if "save_add" in request.POST:
            return redirect(f"{reverse('finance:personal_card_txn_create', args=[card.pk])}?kind={form.cleaned_data['kind']}")
        return redirect("finance:personal_card_detail", pk=card.pk)
    return render(
        request,
        "finance/personal/card_txn_form.html",
        {"form": form, "card": card, "title": f"Add transaction · {card.name}"},
    )


@login_required
def personal_card_entry_edit(request, pk):
    entry = get_object_or_404(CreditCardEntry.objects.select_related("card"), pk=pk)
    form = CreditCardEntryForm(request.POST or None, instance=entry)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Card entry updated.")
        return redirect("finance:personal_card_detail", pk=entry.card_id)
    return render(
        request,
        "finance/personal/card_txn_form.html",
        {"form": form, "card": entry.card, "entry": entry, "title": f"Edit {entry.get_kind_display().lower()}"},
    )


@login_required
@require_POST
def personal_card_entry_delete(request, pk):
    entry = get_object_or_404(CreditCardEntry, pk=pk)
    card_pk = entry.card_id
    entry.delete()
    messages.success(request, "Card entry removed. The outstanding balance has been updated.")
    return redirect("finance:personal_card_detail", pk=card_pk)
