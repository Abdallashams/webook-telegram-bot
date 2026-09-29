
"""
pip install curl_cffi "python-telegram-bot[job-queue]>=21"

Windows (PowerShell):
  $env:ALLOWED_USER_IDS="123456789,987654321"   # اختياري

python webook_telegram_bot_v3.py

الاستخدام:
  /start    -> اختار حدث -> هتظهرلك التذاكر
  اكتب السعر النهائي (بعد الرسوم والضريبة) مثلًا 437.5 -> يعرض التذاكر بنفس السعر
  لو السعر مش موجود -> البوت يراقب الحدث ويبعتلك أول ما تنزل تذكرة بالسعر ده
  /watches  -> عرض المراقبات الشغالة
  /unwatch  -> إلغاء كل المراقبات
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

from curl_cffi import requests

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

from telegram.error import BadRequest

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBOOK_TOKEN = os.getenv("WEBOOK_TOKEN", "").strip()

ALLOWED = {
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN or not WEBOOK_TOKEN:
    raise SystemExit(
        "لازم تحدد TELEGRAM_BOT_TOKEN و WEBOOK_TOKEN"
    )


EVENTS_URL = "https://cdn.webook.com/"

RESALE_BASE_URL = (
    "https://api.webook.com/api/v2/resale-listing"
)

EVENTS_PER_PAGE = 8
TICKETS_PER_PAGE = 5

EVENTS_CACHE_TTL = 300

# =========================================================
# IMPORTANT
# =========================================================

# السعر الذي يكتبه المستخدم يعتبر السعر النهائي
# بعد الرسوم والضريبة.

# مثال:
# المستخدم يكتب 437.5
#
# سيتم اعتبار أي تذكرة نهائية بين:
#
# 432.5
# إلى
# 442.5
#
# مطابقة للمراقبة.

PRICE_TOLERANCE = 5.0


# كل كام ثانية يتم فحص المراقبات
WATCH_INTERVAL = 60

WATCHES_FILE = "watches.json"

# رسوم 10%
FEES_RATE = 0.10

# ضريبة 15%
VAT_RATE = 0.15


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("webook-bot")


# =========================================================
# HEADERS
# =========================================================

EVENT_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://resell.webook.com",
    "referer": "https://resell.webook.com/",
}


RESALE_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://resell.webook.com",
    "referer": "https://resell.webook.com/",
    "token": WEBOOK_TOKEN,
}


# =========================================================
# GRAPHQL QUERY
# =========================================================

QUERY = (
    "query getEventListing("
    "$lang:String,"
    "$limit:Int,"
    "$skip:Int,"
    "$where:EventFilter,"
    "$order:[EventOrder]"
    ")"
    "{"
    "eventCollection("
    "locale:$lang,"
    "limit:$limit,"
    "skip:$skip,"
    "where:$where,"
    "order:$order"
    ")"
    "{"
    "total "
    "items{"
    "__typename "
    "sys{id}"
    "id "
    "title "
    "slug "
    "ticketingUrlSlug "
    "startingPrice "
    "currencyCode "
    "schedule{openDateTime closeDateTime}"
    "buttonLabel "
    "buttonLink "
    "eventType "
    "location{title address city cityCode countryCode}"
    "category{id title slug}"
    "}"
    "}"
    "}"
)


# =========================================================
# WEBOOK
# =========================================================

def build_payload(skip, limit):

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    return {
        "query": QUERY,

        "variables": {
            "order": [
                "order_ASC",
                "sys_publishedAt_DESC",
            ],

            "lang": "en-US",

            "limit": limit,

            "skip": skip,

            "where": {
                "visibility_not": "private",

                "OR": [
                    {
                        "schedule": {
                            "closeDateTime_exists": False
                        }
                    },
                    {
                        "schedule": {
                            "closeDateTime_gte": now
                        }
                    },
                ],

                "AND": [
                    {
                        "visibility_not": "private"
                    },

                    {
                        "showResellBanner": True
                    },

                    {},

                    {},

                    {},

                    {
                        "OR": [
                            {
                                "location": {
                                    "countryCode": "sa"
                                }
                            },
                            {
                                "location": {
                                    "countryCode": "SA"
                                }
                            },
                        ]
                    },
                ],
            },
        },

        "operationName": "getEventListing",
    }


def fetch_events():

    limit = 50
    skip = 0
    events = []

    while True:

        r = requests.post(
            EVENTS_URL,
            headers=EVENT_HEADERS,
            json=build_payload(skip, limit),
            impersonate="chrome",
            timeout=30,
        )

        if r.status_code != 200:
            raise RuntimeError(
                f"Events HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )

        data = r.json()

        if data.get("errors"):
            raise RuntimeError(
                f"GraphQL: {str(data['errors'])[:300]}"
            )

        coll = data["data"]["eventCollection"]

        items = coll.get("items") or []

        events.extend(items)

        if (
            not items
            or len(events) >= coll.get("total", 0)
        ):
            break

        skip += limit

        time.sleep(0.3)

    return events


def event_slug(event):

    return (
        event.get("ticketingUrlSlug")
        or event.get("slug")
    )


# =========================================================
# RESALE LISTINGS
# =========================================================

def fetch_resale_listings(slug):

    listings = []
    page = 1

    while True:

        r = requests.get(
            f"{RESALE_BASE_URL}/{slug}",

            params={
                "lang": "en",
                "visible_in": "resell-webook",
                "page": page,
                "per_page": 100,
            },

            headers=RESALE_HEADERS,

            impersonate="chrome",

            timeout=30,
        )

        if r.status_code in (401, 403):

            raise RuntimeError(
                "توكن ويبوك منتهي أو مرفوض. "
                "جدّده وأعد تشغيل البوت."
            )

        if r.status_code != 200:

            raise RuntimeError(
                f"Resale HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )

        data = r.json()

        chunk = data.get("data") or []

        if isinstance(chunk, dict):

            chunk = (
                chunk.get("items")
                or chunk.get("listings")
                or []
            )

        listings.extend(chunk)

        meta = data.get("meta") or {}

        current = meta.get(
            "current_page",
            page,
        )

        last = meta.get(
            "last_page",
            page,
        )

        if not chunk or current >= last:
            break

        page += 1

        time.sleep(0.5)

    return listings


# =========================================================
# EXTRACT TICKETS
# =========================================================

def extract_tickets(listings):

    tickets = []

    for lst in listings:

        cur = lst.get(
            "currency",
            "SAR",
        )

        for t in lst.get("tickets") or []:

            tickets.append(
                {
                    "section": t.get("section"),

                    "row": t.get("row"),

                    "seat": t.get("seat"),

                    "gate": t.get("gate"),

                    "block": t.get("block"),

                    "resale_price": (
                        t.get("resale_price")
                        or t.get("price")
                    ),

                    "base_price": t.get(
                        "base_price"
                    ),

                    "vat": t.get("vat"),

                    "currency": t.get(
                        "currency",
                        cur,
                    ),

                    "category": (
                        t.get("ticket_category")
                        or {}
                    ).get("title"),

                    "group": (
                        t.get("ticket_group")
                        or {}
                    ).get("title"),

                    "listing_id": lst.get("_id"),
                }
            )

    return tickets


# =========================================================
# HELPERS
# =========================================================

_AR_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩٫٬",
    "0123456789.,",
)


def esc(v):

    return html.escape(
        "-" if v in (None, "") else str(v)
    )


def to_float(v):

    try:
        return float(v)

    except (TypeError, ValueError):

        return None


def parse_price(text):
    """
    يقبل:

    350
    350.5
    ٣٥٠
    1,200
    350 SAR
    350 ريال

    بشرط وجود رقم واحد فقط.
    """

    cleaned = (
        text
        .translate(_AR_DIGITS)
        .replace(",", "")
    )

    nums = re.findall(
        r"\d+(?:\.\d+)?",
        cleaned,
    )

    if len(nums) != 1:
        return None

    return float(nums[0])


def fmt_price(p):

    return (
        f"{p:.2f}"
        .rstrip("0")
        .rstrip(".")
    )


# =========================================================
# PRICE CALCULATION
# =========================================================

def calculate_final_price(resale_price):
    """
    السعر النهائي:

    السعر الأساسي
    + 10% رسوم
    ثم
    + 15% ضريبة على السعر + الرسوم
    """

    p = to_float(resale_price)

    if p is None:
        return None

    after_fees = p + (
        p * FEES_RATE
    )

    final_price = (
        after_fees
        + after_fees * VAT_RATE
    )

    return final_price


def price_difference(
    ticket,
    target_price,
):
    """
    يحسب الفرق بين السعر النهائي
    للتذكرة والسعر الذي طلبه المستخدم.
    """

    final_price = calculate_final_price(
        ticket["resale_price"]
    )

    if final_price is None:
        return None

    return abs(
        final_price - target_price
    )


def price_matches(
    ticket,
    target_price,
):
    """
    هل التذكرة قريبة من السعر المطلوب
    داخل نطاق PRICE_TOLERANCE؟
    """

    difference = price_difference(
        ticket,
        target_price,
    )

    return (
        difference is not None
        and difference <= PRICE_TOLERANCE
    )


def get_closest_ticket(
    tickets,
    target_price,
):
    """
    يرجع أقرب تذكرة للسعر المطلوب
    بشرط أن تكون داخل PRICE_TOLERANCE.
    """

    candidates = []

    for ticket in tickets:

        final_price = calculate_final_price(
            ticket["resale_price"]
        )

        if final_price is None:
            continue

        difference = abs(
            final_price - target_price
        )

        if difference <= PRICE_TOLERANCE:

            candidates.append(
                (
                    difference,
                    final_price,
                    ticket,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0]
    )

    return candidates[0]


def visible_tickets(user_data):

    tickets = user_data.get(
        "tickets"
    ) or []

    price = user_data.get(
        "price"
    )

    if price is None:
        return tickets

    return [
        t
        for t in tickets
        if price_matches(
            t,
            price,
        )
    ]


def available_prices(
    tickets,
    limit=30,
):

    finals = sorted(
        {
            round(
                calculate_final_price(
                    t["resale_price"]
                ),
                2,
            )

            for t in tickets

            if calculate_final_price(
                t["resale_price"]
            ) is not None
        }
    )

    shown = ", ".join(
        fmt_price(p)
        for p in finals[:limit]
    )

    return (
        shown
        + (
            " ..."
            if len(finals) > limit
            else ""
        )
    )


# =========================================================
# TICKET DISPLAY
# =========================================================

def ticket_block(i, t):

    final_p = calculate_final_price(
        t["resale_price"]
    )

    return [

        f"<b>#{i}</b>",

        (
            f"Section: "
            f"{esc(t['section'])} | "
            f"Block: "
            f"{esc(t['block'])}"
        ),

        (
            f"Row: {esc(t['row'])} | "
            f"Seat: {esc(t['seat'])} | "
            f"Gate: {esc(t['gate'])}"
        ),

        (
            f"💰 السعر الأساسي: "
            f"<b>{esc(t['resale_price'])} "
            f"{esc(t['currency'])}</b>"
        ),

        (
            f"✅ السعر النهائي: "
            f"<b>"
            f"{fmt_price(final_p) if final_p else '-'} "
            f"{esc(t['currency'])}"
            f"</b>"
            f" "
            f"(رسوم 10% ثم ضريبة 15%)"
        ),

        (
            f"Base: {esc(t['base_price'])} | "
            f"VAT: {esc(t['vat'])}"
        ),

        (
            f"Category: {esc(t['category'])} | "
            f"Group: {esc(t['group'])}"
        ),

        "",
    ]


# =========================================================
# WATCHES
# =========================================================

def load_watches():

    try:

        with open(
            WATCHES_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)

    except (
        FileNotFoundError,
        ValueError,
    ):

        return []


def save_watches(watches):

    try:

        with open(
            WATCHES_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                watches,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except OSError:

        log.exception(
            "failed saving watches"
        )


# =========================================================
# EVENTS CACHE
# =========================================================

async def get_events_cached(
    context: ContextTypes.DEFAULT_TYPE,
    force=False,
):

    cache = context.bot_data.get(
        "events"
    )

    if (
        not force
        and cache
        and time.time() - cache["ts"]
        < EVENTS_CACHE_TTL
    ):

        return cache["items"]

    items = await asyncio.to_thread(
        fetch_events
    )

    context.bot_data["events"] = {
        "items": items,
        "ts": time.time(),
    }

    return items


# =========================================================
# SAFE EDIT
# =========================================================

async def safe_edit(
    query,
    text,
    markup=None,
):

    try:

        await query.edit_message_text(

            text,

            reply_markup=markup,

            parse_mode="HTML",

            disable_web_page_preview=True,
        )

    except BadRequest as e:

        if "not modified" not in str(e).lower():
            raise


# =========================================================
# AUTHORIZATION
# =========================================================

def authorized(update: Update):

    if not ALLOWED:
        return True

    user = update.effective_user

    return bool(
        user
        and user.id in ALLOWED
    )


# =========================================================
# EVENTS VIEW
# =========================================================

def events_view(
    events,
    page,
):

    total_pages = max(
        1,
        math.ceil(
            len(events)
            / EVENTS_PER_PAGE
        ),
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    start = (
        page
        * EVENTS_PER_PAGE
    )

    chunk = events[
        start:start + EVENTS_PER_PAGE
    ]

    rows = []

    for i, ev in enumerate(
        chunk,
        start=start,
    ):

        city = (
            ev.get("location")
            or {}
        ).get("city") or ""

        label = (
            f"{ev.get('title', 'Unknown')[:40]}"
        )

        if city:
            label += f" | {city}"

        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"ev:{i}",
                )
            ]
        )

    nav = []

    if page > 0:

        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"evp:{page - 1}",
            )
        )

    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="noop",
        )
    )

    if page < total_pages - 1:

        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"evp:{page + 1}",
            )
        )

    rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 تحديث الأحداث",
                callback_data="evr",
            )
        ]
    )

    text = (
        f"🎟 <b>الأحداث المتاحة</b> "
        f"({len(events)})\n"
        f"اختار حدث:"
    )

    return (
        text,
        InlineKeyboardMarkup(rows),
    )


# =========================================================
# TICKETS VIEW
# =========================================================

def tickets_view(
    title,
    tickets,
    page,
    price=None,
    all_count=None,
):

    total = len(tickets)

    total_pages = max(
        1,
        math.ceil(
            total
            / TICKETS_PER_PAGE
        ),
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    start = (
        page
        * TICKETS_PER_PAGE
    )

    chunk = tickets[
        start:start + TICKETS_PER_PAGE
    ]

    lines = [
        f"🎫 <b>{esc(title)}</b>"
    ]

    if price is not None:

        lines.append(
            (
                f"🔎 فلتر السعر النهائي: "
                f"<b>{fmt_price(price)}</b> "
                f"| النتائج: "
                f"{total} من {all_count}"
            )
        )

        lines.append(
            (
                f"📏 نطاق البحث: "
                f"±{fmt_price(PRICE_TOLERANCE)} ريال"
            )
        )

    else:

        lines.append(
            f"إجمالي التذاكر: {total}"
        )

    lines.append(
        f"صفحة {page + 1}/{total_pages}"
    )

    lines.append(
        "💡 اكتب السعر النهائي بعد الرسوم والضريبة"
    )

    lines.append("")

    if not chunk:

        lines.append(
            "مفيش تذاكر مطابقة حاليًا."
        )

    for i, t in enumerate(
        chunk,
        start=start + 1,
    ):

        lines += ticket_block(
            i,
            t,
        )

    nav = []

    if page > 0:

        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"tp:{page - 1}",
            )
        )

    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="noop",
        )
    )

    if page < total_pages - 1:

        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"tp:{page + 1}",
            )
        )

    rows = [nav]

    if price is not None:

        rows.append(
            [
                InlineKeyboardButton(
                    "❌ إلغاء الفلتر",
                    callback_data="tf",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 تحديث",
                callback_data="tr",
            ),

            InlineKeyboardButton(
                "🔙 الأحداث",
                callback_data="evp:0",
            ),
        ]
    )

    return (
        "\n".join(lines),
        InlineKeyboardMarkup(rows),
    )


def current_tickets_view(
    user_data,
    page=0,
):

    return tickets_view(

        user_data["title"],

        visible_tickets(
            user_data
        ),

        page,

        price=user_data.get(
            "price"
        ),

        all_count=len(
            user_data.get(
                "tickets"
            ) or []
        ),
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not authorized(update):

        await update.message.reply_text(
            "مش مسموحلك تستخدم البوت ده."
        )

        return

    msg = await update.message.reply_text(
        "⏳ بجيب الأحداث..."
    )

    try:

        events = await get_events_cached(
            context,
            force=True,
        )

    except Exception as e:

        log.exception(
            "events failed"
        )

        await msg.edit_text(
            f"❌ فشل جلب الأحداث:\n{e}"
        )

        return

    if not events:

        await msg.edit_text(
            "مفيش أحداث متاحة حاليًا."
        )

        return

    text, markup = events_view(
        events,
        0,
    )

    await msg.edit_text(
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )


# =========================================================
# PRICE MESSAGE
# =========================================================

async def on_price_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    """
    أي رسالة نصية = السعر النهائي المطلوب.
    """

    if not authorized(update):
        return

    if context.user_data.get(
        "tickets"
    ) is None:

        await update.message.reply_text(
            "اختار حدث الأول: ابعت /start"
        )

        return

    price = parse_price(
        update.message.text or ""
    )

    if price is None:

        await update.message.reply_text(
            "اكتب سعر واحد بس، مثلًا: 437.5"
        )

        return

    context.user_data["price"] = price

    tickets = context.user_data.get(
        "tickets"
    ) or []

    # =====================================================
    # البحث عن أقرب تذكرة
    # =====================================================

    closest = get_closest_ticket(
        tickets,
        price,
    )

    if closest is None:

        # لا توجد تذكرة داخل النطاق
        context.user_data["price"] = None

        watches = context.bot_data.setdefault(
            "watches",
            [],
        )

        chat_id = update.effective_chat.id

        slug = context.user_data["slug"]

        title = context.user_data["title"]

        exists = any(

            w["chat_id"] == chat_id

            and w["slug"] == slug

            and abs(
                w["price"] - price
            ) <= PRICE_TOLERANCE

            for w in watches
        )

        if not exists:

            watches.append(
                {
                    "chat_id": chat_id,

                    "slug": slug,

                    "title": title,

                    "price": price,
                }
            )

            save_watches(
                watches
            )

        avail = (
            available_prices(
                tickets
            )
            or "-"
        )

        min_price = None

        max_price = None

        all_finals = []

        for t in tickets:

            fp = calculate_final_price(
                t["resale_price"]
            )

            if fp is not None:
                all_finals.append(fp)

        if all_finals:

            min_price = min(
                all_finals
            )

            max_price = max(
                all_finals
            )

        range_text = (
            f"{fmt_price(price - PRICE_TOLERANCE)}"
            f" - "
            f"{fmt_price(price + PRICE_TOLERANCE)}"
        )

        await update.message.reply_text(

            f"❌ مفيش تذكرة قريبة من "
            f"<b>{fmt_price(price)}</b> ريال.\n\n"

            f"🎯 السعر المطلوب: "
            f"<b>{fmt_price(price)}</b> ريال\n"

            f"📏 نطاق المراقبة: "
            f"<b>{range_text}</b> ريال\n\n"

            f"🔔 هراقب الحدث وهبعتلك أول "
            f"تذكرة تدخل النطاق ده.\n\n"

            f"⏱ الفحص كل "
            f"<b>{WATCH_INTERVAL}</b> ثانية.\n\n"

            f"💰 الأسعار الحالية:\n"
            f"{avail}\n\n"

            f"/watches لعرض المراقبات\n"
            f"/unwatch لإلغائها",

            parse_mode="HTML",
        )

        return

    # =====================================================
    # يوجد سعر قريب
    # =====================================================
    # =====================================================
    # يوجد تذاكر مطابقة -> اعرضها كلها
    # =====================================================

    context.user_data["price"] = price

    text, markup = current_tickets_view(
        context.user_data,
        0,
    )

    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# =========================================================
# WATCH LIST
# =========================================================

async def list_watches(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not authorized(update):
        return

    chat_id = update.effective_chat.id

    mine = [
        w
        for w in context.bot_data.get(
            "watches",
            [],
        )

        if w["chat_id"] == chat_id
    ]

    if not mine:

        await update.message.reply_text(
            "مفيش مراقبات شغالة."
        )

        return

    lines = [
        "🔔 <b>المراقبات الشغالة:</b>"
    ]

    for w in mine:

        low = (
            w["price"]
            - PRICE_TOLERANCE
        )

        high = (
            w["price"]
            + PRICE_TOLERANCE
        )

        lines.append(
            (
                f"• {esc(w['title'])}\n"
                f"  🎯 المطلوب: "
                f"<b>{fmt_price(w['price'])}</b>\n"
                f"  📏 النطاق: "
                f"{fmt_price(low)} - "
                f"{fmt_price(high)} ريال"
            )
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# =========================================================
# CLEAR WATCHES
# =========================================================

async def clear_watches(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not authorized(update):
        return

    chat_id = update.effective_chat.id

    watches = context.bot_data.get(
        "watches",
        [],
    )

    before = len(watches)

    watches[:] = [
        w
        for w in watches
        if w["chat_id"] != chat_id
    ]

    save_watches(watches)

    await update.message.reply_text(
        f"تم إلغاء "
        f"{before - len(watches)} "
        f"مراقبة."
    )


# =========================================================
# CHECK WATCHES
# =========================================================

async def check_watches(
    context: ContextTypes.DEFAULT_TYPE
):

    """
    Job دوري:

    1. يأخذ كل المراقبات.
    2. يجمع المراقبات حسب الحدث.
    3. يجلب التذاكر.
    4. يبحث عن أقرب سعر.
    5. لو وجد سعر داخل ±5 ريال:
       يرسل أقرب تذكرة.
    6. يحذف المراقبة بعد الإرسال.
    """

    watches = context.bot_data.get(
        "watches",
        [],
    )

    if not watches:
        return

    by_slug = {}

    for w in list(watches):

        by_slug.setdefault(
            w["slug"],
            [],
        ).append(w)

    done = []

    for slug, ws in by_slug.items():

        try:

            listings = await asyncio.to_thread(
                fetch_resale_listings,
                slug,
            )

        except Exception as e:

            log.warning(
                "watch fetch failed for %s: %s",
                slug,
                e,
            )

            continue

        tickets = extract_tickets(
            listings
        )

        for w in ws:

            closest = get_closest_ticket(
                tickets,
                w["price"],
            )

            if closest is None:
                continue

            difference, final_price, ticket = (
                closest
            )

            lines = [

                "🔔 <b>نزلت تذكرة قريبة من السعر اللي بتدور عليه!</b>",

                f"🎫 {esc(w['title'])}",

                (
                    f"🎯 السعر المطلوب: "
                    f"<b>{fmt_price(w['price'])}</b> ريال"
                ),

                (
                    f"💰 السعر الفعلي بعد الرسوم والضريبة: "
                    f"<b>{fmt_price(final_price)}</b> ريال"
                ),

                (
                    f"📏 الفرق: "
                    f"<b>{fmt_price(difference)}</b> ريال"
                ),

                (
                    f"📌 نطاق المراقبة: "
                    f"{fmt_price(w['price'] - PRICE_TOLERANCE)}"
                    f" - "
                    f"{fmt_price(w['price'] + PRICE_TOLERANCE)} ريال"
                ),

                "",
            ]

            lines += ticket_block(
                1,
                ticket,
            )

            try:

                await context.bot.send_message(

                    w["chat_id"],

                    "\n".join(lines),

                    parse_mode="HTML",

                    disable_web_page_preview=True,
                )

                done.append(w)

                log.info(
                    "Watch matched: %s | target=%s | actual=%s",
                    w["title"],
                    w["price"],
                    final_price,
                )

            except Exception:

                log.exception(
                    "failed sending watch notification"
                )

        await asyncio.sleep(1)

    # =====================================================
    # REMOVE COMPLETED WATCHES
    # =====================================================

    if done:

        for w in done:

            if w in watches:
                watches.remove(w)

        save_watches(
            watches
        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def on_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not authorized(update):

        await query.answer(
            "غير مسموح",
            show_alert=True,
        )

        return

    data = query.data or ""

    await query.answer()

    try:

        # =================================================
        # NOOP
        # =================================================

        if data == "noop":
            return

        # =================================================
        # EVENTS PAGINATION / REFRESH
        # =================================================

        if (
            data.startswith("evp:")
            or data == "evr"
        ):

            events = await get_events_cached(
                context,
                force=(
                    data == "evr"
                ),
            )

            page = (
                int(
                    data.split(":")[1]
                )
                if data.startswith("evp:")
                else 0
            )

            text, markup = events_view(
                events,
                page,
            )

            await safe_edit(
                query,
                text,
                markup,
            )

            return

        # =================================================
        # SELECT EVENT
        # =================================================

        if data.startswith("ev:"):

            events = await get_events_cached(
                context
            )

            idx = int(
                data.split(":")[1]
            )

            if idx >= len(events):

                await safe_edit(
                    query,
                    "الحدث ده اتغير، ابعت /start تاني.",
                )

                return

            ev = events[idx]

            slug = event_slug(ev)

            if not slug:

                await query.answer(
                    "الحدث ده ملوش slug للـ resale",
                    show_alert=True,
                )

                return

            context.user_data["slug"] = slug

            context.user_data["title"] = (
                ev.get(
                    "title",
                    "Event",
                )
            )

            context.user_data["price"] = None

            await load_tickets(
                query,
                context,
            )

            return

        # =================================================
        # REFRESH TICKETS
        # =================================================

        if data == "tr":

            await load_tickets(
                query,
                context,
            )

            return

        # =================================================
        # CLEAR PRICE FILTER
        # =================================================

        if data == "tf":

            context.user_data["price"] = None

            text, markup = (
                current_tickets_view(
                    context.user_data,
                    0,
                )
            )

            await safe_edit(
                query,
                text,
                markup,
            )

            return

        # =================================================
        # TICKETS PAGINATION
        # =================================================

        if data.startswith("tp:"):

            if (
                context.user_data.get(
                    "tickets"
                )
                is None
            ):

                await safe_edit(
                    query,
                    "الجلسة انتهت، ابعت /start تاني.",
                )

                return

            page = int(
                data.split(":")[1]
            )

            text, markup = (
                current_tickets_view(
                    context.user_data,
                    page,
                )
            )

            await safe_edit(
                query,
                text,
                markup,
            )

            return

    except Exception as e:

        log.exception(
            "callback failed"
        )

        await safe_edit(
            query,
            f"❌ حصل خطأ:\n{esc(e)}",
        )


# =========================================================
# LOAD TICKETS
# =========================================================

async def load_tickets(
    query,
    context,
):

    slug = context.user_data["slug"]

    title = context.user_data["title"]

    await safe_edit(
        query,
        (
            f"⏳ بجيب تذاكر "
            f"<b>{esc(title)}</b>..."
        ),
    )

    try:

        listings = await asyncio.to_thread(
            fetch_resale_listings,
            slug,
        )

    except Exception as e:

        log.exception(
            "resale failed"
        )

        back = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 الأحداث",
                        callback_data="evp:0",
                    )
                ]
            ]
        )

        await safe_edit(
            query,
            (
                f"❌ فشل جلب التذاكر:\n"
                f"{esc(e)}"
            ),
            back,
        )

        return

    context.user_data["tickets"] = (
        extract_tickets(
            listings
        )
    )

    text, markup = (
        current_tickets_view(
            context.user_data,
            0,
        )
    )

    await safe_edit(
        query,
        text,
        markup,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # تحميل المراقبات المحفوظة
    app.bot_data["watches"] = (
        load_watches()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "events",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "watches",
            list_watches,
        )
    )

    app.add_handler(
        CommandHandler(
            "unwatch",
            clear_watches,
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            on_button
        )
    )

    # Any text = price
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            on_price_text,
        )
    )

    # Check JobQueue
    if app.job_queue is None:

        raise SystemExit(
            'JobQueue مش متسطب.\n'
            'نفّذ:\n'
            'pip install "python-telegram-bot[job-queue]"'
        )

    # =====================================================
    # WATCH JOB
    # =====================================================

    app.job_queue.run_repeating(
        check_watches,
        interval=WATCH_INTERVAL,
        first=15,
    )

    log.info(
        "Bot is running..."
    )

    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()