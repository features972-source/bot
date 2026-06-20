"""Profit export (/export) — jobs payout breakdown, expenses, net profit."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from config import Settings
from database import (
    ExpenseSpendingEntry,
    PaymentRecord,
    get_expense_spending_by_user,
    get_expense_totals,
    get_payment_totals,
    list_all_payments,
    list_payments_since,
)
from handlers.admin_access import require_admin
from handlers.stats_period import (
    _parse_stats_period,
    current_payment_week_start,
    stats_period_footnote,
)
from money_format import format_amount
from payments_excel_export import (
    CENTRE_PAY_PERCENT,
    FINISHER_PAY_PERCENT,
    STARTER_PAY_PERCENT,
    centre_payout,
    finisher_payout,
    starter_payout,
)

logger = logging.getLogger(__name__)


@dataclass
class UserPayoutEntry:
    user_id: int
    telegram_username: str | None
    display_name: str | None
    starter_amount: float = 0.0
    finisher_amount: float = 0.0
    starter_count: int = 0
    finisher_count: int = 0

    @property
    def total_owed(self) -> float:
        return self.starter_amount + self.finisher_amount


@dataclass
class ProfitExportSummary:
    period_label: str
    payment_count: int
    gross: float
    starter_pay: float
    finisher_pay: float
    centre_pay: float
    expense_count: int
    expense_total: float
    expense_by_user: list[ExpenseSpendingEntry]
    payout_by_user: list[UserPayoutEntry]

    @property
    def net_profit(self) -> float:
        return self.centre_pay - self.expense_total

    @property
    def centre_share_of_gross(self) -> float:
        if self.gross <= 0:
            return 0.0
        return (self.centre_pay / self.gross) * 100

    @property
    def total_owed_to_staff(self) -> float:
        return self.starter_pay + self.finisher_pay


def _aggregate_payouts_by_user(
    payments: list[PaymentRecord],
) -> list[UserPayoutEntry]:
    by_user: dict[int, UserPayoutEntry] = {}

    def _entry(
        user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> UserPayoutEntry:
        if user_id not in by_user:
            by_user[user_id] = UserPayoutEntry(
                user_id=user_id,
                telegram_username=username,
                display_name=display_name,
            )
        entry = by_user[user_id]
        if username and not entry.telegram_username:
            entry.telegram_username = username
        if display_name and not entry.display_name:
            entry.display_name = display_name
        return entry

    for record in payments:
        if record.starter_user_id is not None:
            starter = _entry(
                record.starter_user_id,
                record.starter_username,
                record.starter_display_name,
            )
            starter.starter_amount += starter_payout(record)
            starter.starter_count += 1

        finisher = _entry(
            record.finisher_user_id,
            record.finisher_username,
            record.finisher_display_name,
        )
        finisher.finisher_amount += finisher_payout(record)
        finisher.finisher_count += 1

    return sorted(
        by_user.values(),
        key=lambda row: (-row.total_owed, -row.starter_count - row.finisher_count, row.user_id),
    )


def build_profit_export_handlers() -> list:
    return [CommandHandler("export", export_command)]


def _parse_export_period(args: list[str]) -> tuple:
    if not args:
        return current_payment_week_start()
    since, label = _parse_stats_period(args)
    return since, label


def build_profit_export_summary(
    settings: Settings,
    *,
    since,
    period_label: str,
) -> ProfitExportSummary:
    if since is None:
        payments = list_all_payments(settings.database_path)
    else:
        payments = list_payments_since(settings.database_path, since=since)

    payment_count, gross = get_payment_totals(settings.database_path, since=since)
    expense_count, expense_total = get_expense_totals(settings.database_path, since=since)
    starter_pay = sum(starter_payout(record) for record in payments)
    finisher_pay = sum(finisher_payout(record) for record in payments)
    centre_pay = sum(centre_payout(record) for record in payments)

    return ProfitExportSummary(
        period_label=period_label,
        payment_count=payment_count,
        gross=gross,
        starter_pay=starter_pay,
        finisher_pay=finisher_pay,
        centre_pay=centre_pay,
        expense_count=expense_count,
        expense_total=expense_total,
        expense_by_user=get_expense_spending_by_user(
            settings.database_path, since=since
        ),
        payout_by_user=_aggregate_payouts_by_user(payments),
    )


def _payout_detail(entry: UserPayoutEntry) -> str:
    parts: list[str] = []
    if entry.starter_amount > 0:
        parts.append(f"S {format_amount(entry.starter_amount)}")
    if entry.finisher_amount > 0:
        parts.append(f"F {format_amount(entry.finisher_amount)}")
    return " + ".join(parts) if parts else "—"


def format_profit_export_caption(summary: ProfitExportSummary, *, bot_name: str) -> str:
    lines = [
        f"📊 <b>{html.escape(bot_name)} — profit export</b>",
        f"<i>{html.escape(summary.period_label.capitalize())}</i>",
        "",
        f"<b>Jobs:</b> {format_amount(summary.gross)} ({summary.payment_count} payments)",
        f"<b>Starter ({STARTER_PAY_PERCENT}%):</b> {format_amount(summary.starter_pay)}",
        f"<b>Finisher ({FINISHER_PAY_PERCENT}%):</b> {format_amount(summary.finisher_pay)}",
        f"<b>Centre ({CENTRE_PAY_PERCENT}%):</b> {format_amount(summary.centre_pay)}",
        "",
        f"<b>Expenses:</b> {format_amount(summary.expense_total)} ({summary.expense_count} items)",
        f"<b>Net profit</b> (centre − expenses): <b>{format_amount(summary.net_profit)}</b>",
        "",
    ]
    if summary.payout_by_user:
        lines.append(
            f"<b>Owed to staff:</b> {format_amount(summary.total_owed_to_staff)}"
        )
        for entry in summary.payout_by_user[:12]:
            label = entry.telegram_username or entry.display_name or str(entry.user_id)
            if entry.telegram_username and not label.startswith("@"):
                label = f"@{label.lstrip('@')}"
            lines.append(
                f"  · {html.escape(str(label))}: "
                f"<b>{format_amount(entry.total_owed)}</b> "
                f"({html.escape(_payout_detail(entry))})"
            )
        if len(summary.payout_by_user) > 12:
            lines.append(f"  · … +{len(summary.payout_by_user) - 12} more")
        lines.append("")
    lines.append(stats_period_footnote())
    return "\n".join(lines)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    message = update.effective_message
    if not message:
        return

    since, period_label = _parse_export_period(context.args or [])
    summary = build_profit_export_summary(
        settings, since=since, period_label=period_label
    )

    from handlers.profit_export_image import (
        profit_export_input_file,
        render_profit_export_png,
    )

    try:
        image_bytes = await asyncio.to_thread(
            render_profit_export_png,
            summary,
            database_path=settings.database_path,
            bot_display_name=settings.bot_display_name,
        )
    except Exception:
        logger.exception("Failed to render profit export image")
        await message.reply_text(
            format_profit_export_caption(summary, bot_name=settings.bot_display_name),
            parse_mode="HTML",
        )
        return

    caption = format_profit_export_caption(summary, bot_name=settings.bot_display_name)
    await message.reply_photo(
        photo=profit_export_input_file(image_bytes),
        caption=caption,
        parse_mode="HTML",
    )
