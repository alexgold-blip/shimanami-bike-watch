"""Interactive, multi-user Telegram bot for Shimanami rental-cycle availability.

Anyone can talk to the bot and set their own watches (date + terminal + bike
type). A background job polls the public stock API every CHECK_INTERVAL_MIN
minutes and notifies each subscriber when their bike becomes available.

Run:  TELEGRAM_BOT_TOKEN=... python bot.py
"""

import datetime as dt
import logging
import os

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from shimanami_data import CYCLE_LABEL, CYCLE_TYPES, TERMINAL_LABEL, TERMINALS
from store import Store

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("shimanami-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHECK_INTERVAL_MIN = float(os.getenv("CHECK_INTERVAL_MIN", "10"))
DB_PATH = os.getenv("DB_PATH", "data/shimanami.db")
STOCK_API = "https://shimanami.sports.navitime.jp/shimanami/bookings/stocks"
BOOKING_URL = "https://www.shimanami-bike-rental.com/booking/term"

# Chat ids allowed to use /admin (comma-separated). Empty = nobody.
ADMIN_CHAT_IDS = {
    int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x
}


def is_admin(chat_id) -> bool:
    return chat_id in ADMIN_CHAT_IDS

store = Store(DB_PATH)

# Conversation states for the /watch wizard.
PICK_TERMINAL, PICK_CYCLE, ENTER_DATES = range(3)


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------
async def fetch_stocks(dates):
    """Return {iso_date: {(port_id, cycle_type): count}} for the given dates."""
    start, end = min(dates), max(dates)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            STOCK_API,
            params={"start": start, "end": end},
            headers={"User-Agent": "shimanami-bike-watch-bot/1.0"},
        )
        r.raise_for_status()
        data = r.json()

    out = {}
    for day in data:
        y, m, d = day["date"]
        iso = f"{y:04d}-{m:02d}-{d:02d}"
        table = {}
        for item in day.get("availables", []):
            key = (str(item["port"]["id"]), item["cycle"]["type"])
            table[key] = int(item.get("availableCount", 0))
        out[iso] = table
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def valid_date(token: str):
    try:
        return dt.date.fromisoformat(token).isoformat()
    except ValueError:
        return None


def parse_dates(text: str):
    """Accept '2026-10-15', comma lists, and 'start..end' ranges."""
    result = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if ".." in chunk:
            a, b = chunk.split("..", 1)
            da, db = valid_date(a), valid_date(b)
            if not da or not db:
                return None
            cur, last = dt.date.fromisoformat(da), dt.date.fromisoformat(db)
            if last < cur or (last - cur).days > 60:
                return None
            while cur <= last:
                result.append(cur.isoformat())
                cur += dt.timedelta(days=1)
        else:
            iso = valid_date(chunk)
            if not iso:
                return None
            result.append(iso)
    # De-duplicate, keep order.
    seen, ordered = set(), []
    for d in result:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered or None


def sub_line(sub) -> str:
    term = TERMINAL_LABEL.get(sub["port_id"], sub["port_id"])
    bike = CYCLE_LABEL.get(sub["cycle_type"], sub["cycle_type"])
    count = sub["last_count"]
    if count is None:
        state = "not checked yet"
    elif count < 0:
        state = "not offered here"
    elif count == 0:
        state = "sold out"
    else:
        state = f"✅ {count} available"
    return f"• {sub['date']} — {bike} @ {term}: {state}"


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚲 *Shimanami Rental-Cycle Watch*\n\n"
        "Я слежу за наличием велосипедов на сайте бронирования и пишу, "
        "когда нужный появляется.\n\n"
        "Команды:\n"
        "/watch — добавить отслеживание (терминал, велосипед, даты)\n"
        "/list — мои отслеживания\n"
        "/status — текущее наличие по моим отслеживаниям\n"
        "/stop — удалить отслеживания\n"
        "/help — помощь",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n\n"
        "1. /watch — бот спросит терминал, тип велосипеда и даты.\n"
        "2. Даты вводи в формате ГГГГ-ММ-ДД. Можно несколько через запятую "
        "(2026-10-14,2026-10-15) или диапазоном (2026-10-14..2026-10-16).\n"
        "3. Когда велосипед появится, придёт уведомление.\n\n"
        "/status — посмотреть, что сейчас свободно/разобрано и когда была "
        "последняя проверка.\n"
        "/whoami — узнать свой chat_id.\n"
        f"Проверка идёт автоматически каждые {int(CHECK_INTERVAL_MIN)} мин.",
    )


# --------------------------------------------------------------------------
# /watch conversation
# --------------------------------------------------------------------------
async def watch_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows, row = [], []
    for pid, en, jp in TERMINALS:
        row.append(InlineKeyboardButton(f"{jp} {en}", callback_data=f"term:{pid}"))
        if len(row) == 1:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖ Отмена", callback_data="cancel")])
    await update.message.reply_text(
        "1/3 — выбери терминал получения:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_TERMINAL


async def watch_pick_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await watch_cancel_cb(update, ctx)
    ctx.user_data["port_id"] = q.data.split(":", 1)[1]

    rows = [
        [InlineKeyboardButton(en, callback_data=f"cyc:{i}")]
        for i, (jp, en) in enumerate(CYCLE_TYPES)
    ]
    rows.append([InlineKeyboardButton("✖ Отмена", callback_data="cancel")])
    await q.edit_message_text(
        f"Терминал: {TERMINAL_LABEL[ctx.user_data['port_id']]}\n\n"
        "2/3 — выбери тип велосипеда:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_CYCLE


async def watch_pick_cycle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await watch_cancel_cb(update, ctx)
    idx = int(q.data.split(":", 1)[1])
    jp, en = CYCLE_TYPES[idx]
    ctx.user_data["cycle_type"] = jp
    await q.edit_message_text(
        f"Терминал: {TERMINAL_LABEL[ctx.user_data['port_id']]}\n"
        f"Велосипед: {en}\n\n"
        "3/3 — введи дату(ы) в формате ГГГГ-ММ-ДД.\n"
        "Примеры:\n"
        "  2026-10-15\n"
        "  2026-10-14,2026-10-15,2026-10-16\n"
        "  2026-10-14..2026-10-16",
    )
    return ENTER_DATES


async def watch_enter_dates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dates = parse_dates(update.message.text)
    if not dates:
        await update.message.reply_text(
            "Не понял даты. Формат ГГГГ-ММ-ДД, например 2026-10-15. Попробуй ещё раз."
        )
        return ENTER_DATES

    chat_id = update.effective_chat.id
    port_id = ctx.user_data["port_id"]
    cycle_type = ctx.user_data["cycle_type"]
    added = sum(
        store.add_subscription(chat_id, port_id, cycle_type, d) for d in dates
    )
    bike = CYCLE_LABEL[cycle_type]
    term = TERMINAL_LABEL[port_id]
    await update.message.reply_text(
        f"Готово. Слежу за *{bike}* @ {term} на {len(dates)} дат "
        f"(новых добавлено: {added}).\n\n"
        "Проверю в ближайшем цикле и напишу, когда появится. "
        "Текущее наличие — /status.",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data.clear()
    # Kick an immediate check so /status is fresh right away.
    ctx.application.create_task(run_check(ctx.application))
    return ConversationHandler.END


async def watch_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Отменено.")
    ctx.user_data.clear()
    return ConversationHandler.END


async def watch_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    ctx.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------
# /list and /stop
# --------------------------------------------------------------------------
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("У тебя пока нет отслеживаний. Добавь через /watch.")
        return
    text = "Твои отслеживания:\n" + "\n".join(sub_line(s) for s in subs)
    await update.message.reply_text(text)


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("У тебя нет отслеживаний.")
        return
    rows = [
        [InlineKeyboardButton(f"🗑 {s['date']} · {CYCLE_LABEL.get(s['cycle_type'], s['cycle_type'])} · "
                              f"{TERMINAL_LABEL.get(s['port_id'], s['port_id']).split(' (')[0]}",
                              callback_data=f"del:{s['id']}")]
        for s in subs
    ]
    rows.append([InlineKeyboardButton("🗑 Удалить все", callback_data="delall")])
    await update.message.reply_text(
        "Что удалить?", reply_markup=InlineKeyboardMarkup(rows)
    )


async def stop_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    if q.data == "delall":
        n = store.clear_subscriptions(chat_id)
        await q.edit_message_text(f"Удалено отслеживаний: {n}.")
        return
    sub_id = int(q.data.split(":", 1)[1])
    ok = store.delete_subscription(chat_id, sub_id)
    await q.edit_message_text("Удалено." if ok else "Уже удалено.")


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("Нет отслеживаний. Добавь через /watch.")
        return
    last = store.get_meta("last_check_iso")
    header = f"Последняя проверка: {last} UTC\n" if last else "Ещё не проверялось.\n"
    text = header + "\n".join(sub_line(s) for s in subs)
    await update.message.reply_text(text)


# --------------------------------------------------------------------------
# /whoami and /admin
# --------------------------------------------------------------------------
async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    role = "админ" if is_admin(cid) else "пользователь"
    # Plain text on purpose: the id and the variable name contain underscores,
    # which legacy Markdown would misread as italic markers.
    await update.message.reply_text(
        f"Твой chat_id: {cid}\nРоль: {role}\n\n"
        "Чтобы стать админом, добавь это число в переменную окружения "
        "ADMIN_CHAT_IDS на хостинге и передеплой."
    )


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if not is_admin(cid):
        await update.message.reply_text(
            "Команда только для админа. Узнать свой id — /whoami."
        )
        return

    subs = store.all_subscriptions()
    last = store.get_meta("last_check_iso")
    users = len({s["chat_id"] for s in subs})

    header = (
        "📊 *Сводка бота*\n"
        f"Пользователей: {users}\n"
        f"Отслеживаний: {len(subs)}\n"
        f"Последняя проверка: {last or '—'} UTC\n"
    )

    if not subs:
        await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)
        return

    # Aggregate by (bike @ terminal, date): how many watchers + current count.
    groups = {}
    for s in subs:
        key = (s["cycle_type"], s["port_id"], s["date"])
        g = groups.setdefault(key, {"watchers": 0, "count": s["last_count"]})
        g["watchers"] += 1
        if s["last_count"] is not None:
            g["count"] = s["last_count"]

    lines = ["", "*По отслеживаниям:*"]
    for (cyc, port, date), g in sorted(groups.items(), key=lambda kv: kv[0][2]):
        bike = CYCLE_LABEL.get(cyc, cyc)
        term = TERMINAL_LABEL.get(port, port).split(" (")[0]
        c = g["count"]
        state = (
            "не проверено" if c is None
            else "нет в наличии" if c < 0
            else "разобрано" if c == 0
            else f"✅ {c} шт."
        )
        lines.append(f"• {date} · {bike} @ {term}: {state} (следят: {g['watchers']})")

    text = header + "\n".join(lines)
    # Telegram messages cap at 4096 chars; trim defensively.
    await update.message.reply_text(text[:4000], parse_mode=ParseMode.MARKDOWN)


# --------------------------------------------------------------------------
# Background availability check
# --------------------------------------------------------------------------
async def run_check(app: Application):
    subs = store.all_subscriptions()
    if not subs:
        store.set_meta("last_check_iso", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
        return

    dates = sorted({s["date"] for s in subs})
    try:
        stocks = await fetch_stocks(dates)
    except Exception as exc:  # network / API hiccup: skip this cycle quietly
        log.warning("stock fetch failed: %s", exc)
        return

    store.set_meta("last_check_iso", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"))

    for s in subs:
        table = stocks.get(s["date"], {})
        count = table.get((s["port_id"], s["cycle_type"]), -1)
        was_notified = bool(s["notified"])

        if count > 0 and not was_notified:
            bike = CYCLE_LABEL.get(s["cycle_type"], s["cycle_type"])
            term = TERMINAL_LABEL.get(s["port_id"], s["port_id"])
            try:
                await app.bot.send_message(
                    chat_id=s["chat_id"],
                    text=(
                        f"🚲 ПОЯВИЛСЯ!\n\n"
                        f"{bike} @ {term}\n"
                        f"Дата: {s['date']}\n"
                        f"Свободно: {count} шт.\n\n"
                        f"Бронируй: {BOOKING_URL}"
                    ),
                )
            except Exception as exc:
                log.warning("send to %s failed: %s", s["chat_id"], exc)
            store.update_state(s["id"], count, notified=1)
        elif count <= 0 and was_notified:
            # Sold out again -> re-arm so the next opening notifies once more.
            store.update_state(s["id"], count, notified=0)
        else:
            store.update_state(s["id"], count, s["notified"])


async def job_check(ctx: ContextTypes.DEFAULT_TYPE):
    await run_check(ctx.application)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("watch", watch_start)],
        states={
            PICK_TERMINAL: [CallbackQueryHandler(watch_pick_terminal)],
            PICK_CYCLE: [CallbackQueryHandler(watch_pick_cycle)],
            ENTER_DATES: [MessageHandler(filters.TEXT & ~filters.COMMAND, watch_enter_dates)],
        },
        fallbacks=[CommandHandler("cancel", watch_cancel_cmd)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern=r"^(del:|delall)"))

    app.job_queue.run_repeating(
        job_check, interval=CHECK_INTERVAL_MIN * 60, first=10
    )

    log.info("Bot started. Check interval = %s min.", CHECK_INTERVAL_MIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
