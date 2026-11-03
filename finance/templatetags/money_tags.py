from django import template

from finance.money import format_inr
from finance.services import (
    DEBT_OPEN,
    DEBT_SETTLED,
    INSTALLMENT_PARTLY_PAID,
    EMI_DUE,
    EMI_OVERDUE,
    EMI_PAID,
    EMI_UPCOMING,
    STATUS_FULL,
    STATUS_NEAR,
    STATUS_OVER,
    STATUS_UNBUDGETED,
    STATUS_WITHIN,
)

register = template.Library()


@register.filter
def inr(value):
    return format_inr(value)


@register.filter
def status_badge(status):
    mapping = {
        STATUS_WITHIN: "bg-success-subtle text-success-emphasis",
        STATUS_NEAR: "bg-warning-subtle text-warning-emphasis",
        STATUS_FULL: "bg-primary-subtle text-primary-emphasis",
        STATUS_OVER: "bg-danger-subtle text-danger-emphasis",
        STATUS_UNBUDGETED: "bg-secondary-subtle text-secondary-emphasis",
    }
    return mapping.get(status, "bg-light text-dark")


@register.filter
def emi_badge(state):
    mapping = {
        EMI_PAID: "bg-success-subtle text-success-emphasis",
        EMI_DUE: "bg-warning-subtle text-warning-emphasis",
        EMI_OVERDUE: "bg-danger-subtle text-danger-emphasis",
        EMI_UPCOMING: "bg-secondary-subtle text-secondary-emphasis",
        INSTALLMENT_PARTLY_PAID: "bg-info-subtle text-info-emphasis",
        DEBT_SETTLED: "bg-success-subtle text-success-emphasis",
        DEBT_OPEN: "bg-primary-subtle text-primary-emphasis",
    }
    return mapping.get(state, "bg-light text-dark")
