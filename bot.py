#!/usr/bin/env python3
"""Telegram bot that splits a freight rate by route distance and NBU FX rate."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import httpx
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


LOGGER = logging.getLogger("route_split_bot")
MONEY = Decimal("0.01")
RATE_PRECISION = Decimal("0.0001")


class UserInputError(ValueError):
    """Raised when the user request cannot be parsed."""


class ExternalServiceError(RuntimeError):
    """Raised when an external API cannot provide a usable response."""


@dataclass(frozen=True)
class TripRequest:
    amount: Decimal
    currency: str
    origin: str
    border: str
    destination: str


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    label: str


@dataclass(frozen=True)
class MoneySplit:
    first: Decimal
    second: Decimal
    total: Decimal


@dataclass(frozen=True)
class Calculation:
    request: TripRequest
    rate: Decimal
    rate_date: str
    origin: GeoPoint
    border: GeoPoint
    destination: GeoPoint
    first_km: Decimal
    second_km: Decimal
    foreign: MoneySplit
    hryvnia: MoneySplit


# Aliases improve the chance of locating the checkpoint itself instead of a town
# with the same name. The final point returned by the geocoder is always shown to
# the user so that it can be verified before using the result in documents.
BORDER_ALIASES: dict[str, tuple[str, ...]] = {
    "ягодин": (
        "Przejście graniczne Dorohusk-Jagodzin",
        "автомобільний пункт пропуску Ягодин Дорогуськ",
    ),
    "yagodyn": ("Przejście graniczne Dorohusk-Jagodzin",),
    "jagodzin": ("Przejście graniczne Dorohusk-Jagodzin",),
    "dorohusk": ("Przejście graniczne Dorohusk-Jagodzin",),
    "ужгород": (
        "Ужгород пункт пропуску",
        "автомобільний пункт пропуску Ужгород Вишнє Нємецьке",
    ),
    "uzhhorod": ("Ужгород пункт пропуску",),
    "вишнє нємецьке": ("Ужгород пункт пропуску",),
    "vysne nemecke": ("Ужгород пункт пропуску",),
    "краківець": ("Przejście graniczne Korczowa Krakowiec",),
    "краковець": ("Przejście graniczne Korczowa Krakowiec",),
    "korczowa": ("Przejście graniczne Korczowa Krakowiec",),
    "шегині": ("Przejście graniczne Medyka Szeginie",),
    "medyka": ("Przejście graniczne Medyka Szeginie",),
    "рава руська": ("Przejście graniczne Hrebenne Rawa Ruska",),
    "hrebenne": ("Przejście graniczne Hrebenne Rawa Ruska",),
    "чоп": ("Пункт пропуску Чоп Тиса",),
    "zahony": ("Пункт пропуску Чоп Тиса",),
    "порубне": ("Пункт пропуску Порубне",),
    "siret": ("Пункт пропуску Порубне",),
    "мамалига": ("Пункт пропуску Мамалига",),
    "орлівка": ("Orlivka ferry terminal Ukraine",),
}


def _border_point(latitude: float, longitude: float, label: str) -> GeoPoint:
    return GeoPoint(latitude=latitude, longitude=longitude, label=label)


# Fixed checkpoint coordinates make the most common freight crossings reliable
# and prevent a geocoder from choosing a same-named village or railway station.
_YAHODYN = _border_point(51.1816966, 23.8032911, "МАПП Ягодин — Дорогуськ")
_UZHHOROD = _border_point(48.6535970, 22.2660671, "МАПП Ужгород — Вишнє Нємецьке")
_KRAKOVETS = _border_point(49.9557183, 23.1114930, "МАПП Краківець — Корчова")
_SHEHYNI = _border_point(49.7999830, 22.9447916, "МАПП Шегині — Медика")
_RAVA_RUSKA = _border_point(50.2765201, 23.5882751, "МАПП Рава-Руська — Гребенне")
_USTYLUH = _border_point(50.8593346, 24.1354769, "МАПП Устилуг — Зосин")
_UHRYNIV = _border_point(50.5781386, 24.0871830, "МАПП Угринів — Долгобичув")
_SMILNYTSIA = _border_point(49.4814301, 22.6982873, "МАПП Смільниця — Кросценко")
_CHOP = _border_point(48.4177360, 22.1699165, "МАПП Чоп (Тиса) — Захонь")
_PORUBNE = _border_point(47.9889112, 26.0600339, "МАПП Порубне — Сірет")
_MAMALYHA = _border_point(48.2643853, 26.6231899, "МАПП Мамалига — Крива")
_ORLIVKA = _border_point(45.2911007, 28.4584878, "Поромний перехід Орлівка — Ісакча")

BORDER_POINTS: dict[str, GeoPoint] = {
    **{key: _YAHODYN for key in ("ягодин", "yagodyn", "jagodzin", "дорогуськ", "dorohusk")},
    **{key: _UZHHOROD for key in ("ужгород", "uzhhorod", "вишнє нємецьке", "vysne nemecke")},
    **{key: _KRAKOVETS for key in ("краківець", "краковець", "krakovets", "korczowa")},
    **{key: _SHEHYNI for key in ("шегині", "shehyni", "medyka")},
    **{key: _RAVA_RUSKA for key in ("рава руська", "rava ruska", "hrebenne")},
    **{key: _USTYLUH for key in ("устилуг", "ustyluh", "зосин", "zosin")},
    **{key: _UHRYNIV for key in ("угринів", "uhryniv", "долгобичув", "dolhobyczow")},
    **{key: _SMILNYTSIA for key in ("смільниця", "smilnytsia", "кросценко", "kroscienko")},
    **{key: _CHOP for key in ("чоп", "chop", "тиса", "tisa", "záhony", "zahony")},
    **{key: _PORUBNE for key in ("порубне", "porubne", "сірет", "siret")},
    **{key: _MAMALYHA for key in ("мамалига", "mamalyha", "крива", "criva")},
    **{key: _ORLIVKA for key in ("орлівка", "orlivka", "ісакча", "isaccea")},
}


HELP_TEXT = """<b>Розподіл ставки за кілометражем</b>

Натисніть /start — бот по черзі запитає:
1. місце завантаження;
2. перехід;
3. місце розмитнення;
4. місце вивантаження;
5. ставку.

Ставку можна написати так:
<code>4500е</code>, <code>4500 євро</code>, <code>3500дол</code> або <code>3500$</code>.

Швидкий запит одним повідомленням також працює:
<code>Гданськ - Ягодин - Київ 3500 дол</code>

Команди:
/start — новий покроковий розрахунок
/cancel — скасувати заповнення
Стоп або /stop — скинути помилкові дані та почати спочатку
/rate — поточний курс USD та EUR за НБУ
/id — ваш Telegram ID
/help — ця підказка

Відстань розраховується автомобільними дорогами. Перед внесенням у рахунок перевірте знайдені міста та пункт пропуску.

Дані маршруту: © OpenStreetMap contributors, OSRM."""


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-zа-яіїєґ0-9]+", " ", without_marks).strip()


def parse_decimal(value: str, field_name: str) -> Decimal:
    raw = value.strip().replace("\u00a0", "").replace(" ", "")
    raw = re.sub(r"[^0-9,.-]", "", raw)
    if not raw:
        raise UserInputError(f"Не бачу число в полі «{field_name}».")

    if "," in raw and "." in raw:
        # Whichever separator appears last is considered the decimal separator.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(",") == 1:
        left, right = raw.split(",")
        raw = left + right if len(right) == 3 and len(left) <= 3 else left + "." + right
    elif raw.count(".") == 1:
        left, right = raw.split(".")
        raw = left + right if len(right) == 3 and len(left) <= 3 else raw
    else:
        raw = raw.replace(",", "").replace(".", "")

    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise UserInputError(f"Невірне число в полі «{field_name}».") from exc
    if number <= 0:
        raise UserInputError(f"Поле «{field_name}» має бути більше нуля.")
    return number


def parse_amount_currency(value: str) -> tuple[Decimal, str]:
    lowered = value.casefold()
    if "$" in value or re.search(r"\b(?:usd|дол(?:ар(?:ів|и)?|лар(?:ов|а|ы)?)?)\b", lowered):
        currency = "USD"
    elif ("€" in value or re.search(r"\b(?:eur|euro|євро|евро)\b", lowered)
          or re.search(r"\d[\d\s\u00a0.,]*\s*[еє]\s*$", lowered)):
        currency = "EUR"
    else:
        raise UserInputError("Після суми вкажіть валюту: USD/$ або EUR/€.")

    amount_match = re.search(r"\d[\d\s\u00a0.,]*", value)
    if not amount_match:
        raise UserInputError("Не бачу суму ставки.")
    return parse_decimal(amount_match.group(0), "сума"), currency


def strip_command(text: str) -> str:
    return re.sub(r"^/[a-zA-Z_]+(?:@[a-zA-Z0-9_]+)?\s*", "", text.strip(), count=1)


def parse_trip_request(text: str) -> TripRequest:
    payload = strip_command(text)

    # Keep compatibility with the original pipe-separated command.
    if "|" in payload:
        parts = [part.strip() for part in payload.split("|")]
        if len(parts) == 4 and all(parts):
            amount, currency = parse_amount_currency(parts[0])
            return TripRequest(amount, currency, parts[1], parts[2], parts[3])

    # Natural format: origin - checkpoint - destination amount currency.
    # A regular hyphen is treated as a separator only when surrounded by spaces,
    # so names such as Івано-Франківськ remain intact.
    route_parts = re.split(r"\s+(?:-|–|—|→)\s+", payload, maxsplit=2)
    if len(route_parts) != 3:
        route_parts = re.split(r"\s*(?:–|—|→)\s*", payload, maxsplit=2)

    if len(route_parts) == 3:
        origin, border, destination_and_rate = (part.strip() for part in route_parts)
        rate_match = re.search(
            r"(?P<rate>(?:[$€]\s*)?\d[\d\s\u00a0.,]*\s*"
            r"(?:USD|EUR|дол(?:ар(?:ів|и)?|лар(?:ів|ов|а|ы)?)?|євро|евро|[еє]|[$€]))\s*$",
            destination_and_rate,
            flags=re.IGNORECASE,
        )
        if rate_match:
            destination = destination_and_rate[: rate_match.start()].strip(" ,")
            rate_text = rate_match.group("rate")
            if origin and border and destination:
                amount, currency = parse_amount_currency(rate_text)
                return TripRequest(amount, currency, origin, border, destination)

    raise UserInputError(
        "Напишіть маршрут і ставку так:\n"
        "Гданськ - Ягодин - Київ 3500 дол"
    )


def parse_manual_request(text: str) -> tuple[Decimal, str, Decimal, Decimal]:
    payload = strip_command(text)
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) != 3 or any(not part for part in parts):
        raise UserInputError(
            "Формат ручного розрахунку:\n"
            "/manual сума і валюта | км до кордону | км після кордону"
        )
    amount, currency = parse_amount_currency(parts[0])
    return (
        amount,
        currency,
        parse_decimal(parts[1], "км до кордону"),
        parse_decimal(parts[2], "км після кордону"),
    )


def split_total(total: Decimal, first_km: Decimal, second_km: Decimal) -> MoneySplit:
    all_km = first_km + second_km
    if all_km <= 0:
        raise UserInputError("Загальний кілометраж має бути більше нуля.")
    first = (total * first_km / all_km).quantize(MONEY, rounding=ROUND_HALF_UP)
    second = total.quantize(MONEY, rounding=ROUND_HALF_UP) - first
    return MoneySplit(first=first, second=second, total=first + second)


def money(value: Decimal) -> str:
    formatted = f"{value.quantize(MONEY, rounding=ROUND_HALF_UP):,.2f}"
    return formatted.replace(",", " ").replace(".", ",")


def number(value: Decimal, places: int = 1) -> str:
    quantizer = Decimal("1") if places == 0 else Decimal("1." + ("0" * places))
    formatted = f"{value.quantize(quantizer, rounding=ROUND_HALF_UP):,.{places}f}"
    return formatted.replace(",", " ").replace(".", ",")


def border_queries(border: str) -> tuple[str, ...]:
    key = normalize_key(border)
    for alias, queries in BORDER_ALIASES.items():
        if key == alias or alias in key:
            return queries
    return (f"road border crossing {border}, Ukraine", border)


def known_border_point(border: str) -> GeoPoint | None:
    key = normalize_key(border)
    for alias, point in BORDER_POINTS.items():
        if key == normalize_key(alias) or normalize_key(alias) in key:
            return point
    return None


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    service_name: str,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.7)
    raise ExternalServiceError(f"Сервіс «{service_name}» зараз не відповідає. Спробуйте ще раз пізніше.") from last_error


class NBUClient:
    def __init__(self) -> None:
        self.url = os.getenv(
            "NBU_API_URL",
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchangeNew",
        ).rstrip("/")
        self.user_agent = os.getenv("HTTP_USER_AGENT", "nbu-route-split-bot/1.0")

    async def rate(self, currency: str) -> tuple[Decimal, str]:
        if currency not in {"USD", "EUR"}:
            raise UserInputError("Підтримуються тільки USD та EUR.")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": self.user_agent},
        ) as client:
            data = await get_json(
                client,
                self.url,
                params={"valcode": currency, "json": ""},
                service_name="НБУ",
            )
        if not isinstance(data, list) or not data:
            raise ExternalServiceError("НБУ не повернув курс для цієї валюти.")
        try:
            item = data[0]
            rate = Decimal(str(item["rate"])).quantize(RATE_PRECISION)
            rate_date = str(item.get("exchangedate") or item.get("exchangeDate") or "—")
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ExternalServiceError("Відповідь НБУ має невідомий формат.") from exc
        return rate, rate_date


class GeoRouter:
    def __init__(self) -> None:
        self.nominatim_url = os.getenv(
            "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
        ).rstrip("/")
        self.osrm_url = os.getenv("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
        self.user_agent = os.getenv(
            "HTTP_USER_AGENT", "nbu-route-split-bot/1.0 (personal Telegram bot)"
        )
        self.contact_email = os.getenv("CONTACT_EMAIL", "").strip()
        self._cache: dict[str, GeoPoint] = {}
        self._geocode_lock = asyncio.Lock()
        self._last_geocode_at = 0.0

    async def geocode(self, query: str) -> GeoPoint | None:
        cache_key = normalize_key(query)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # The public Nominatim policy permits at most one request per second.
        async with self._geocode_lock:
            cached = self._cache.get(cache_key)
            if cached:
                return cached
            wait_for = 1.05 - (time.monotonic() - self._last_geocode_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)

            params = {
                "q": query,
                "format": "jsonv2",
                "limit": "5",
                "addressdetails": "1",
                "accept-language": "uk,en",
            }
            if self.contact_email:
                params["email"] = self.contact_email
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                headers={"User-Agent": self.user_agent},
            ) as client:
                try:
                    data = await get_json(
                        client,
                        f"{self.nominatim_url}/search",
                        params=params,
                        service_name="геокодування",
                    )
                finally:
                    self._last_geocode_at = time.monotonic()

            if not isinstance(data, list) or not data:
                return None
            best = max(data, key=self._geocode_score)
            try:
                point = GeoPoint(
                    latitude=float(best["lat"]),
                    longitude=float(best["lon"]),
                    label=str(best.get("display_name") or query),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ExternalServiceError("Геокодер повернув невідомий формат координат.") from exc
            self._cache[cache_key] = point
            return point

    @staticmethod
    def _geocode_score(item: dict[str, Any]) -> float:
        score = float(item.get("importance") or 0.0)
        category = str(item.get("category") or item.get("class") or "").casefold()
        item_type = str(item.get("type") or "").casefold()
        label = str(item.get("display_name") or "").casefold()
        if category == "barrier" or item_type in {"border_control", "checkpoint"}:
            score += 10.0
        if any(word in label for word in ("border", "кордон", "пункт пропуску")):
            score += 5.0
        return score

    async def resolve_place(self, query: str, field_name: str) -> GeoPoint:
        point = await self.geocode(query)
        if point is None:
            raise UserInputError(
                f"Не вдалося знайти «{query}» ({field_name}). Додайте країну або область."
            )
        return point

    async def resolve_border(self, query: str) -> GeoPoint:
        known = known_border_point(query)
        if known is not None:
            return known
        for candidate in border_queries(query):
            point = await self.geocode(candidate)
            if point is not None:
                return point
        raise UserInputError(
            f"Не вдалося знайти перехід «{query}». Вкажіть обидві назви, наприклад Ягодин–Дорогуськ."
        )

    async def distance_km(self, start: GeoPoint, finish: GeoPoint) -> Decimal:
        coordinates = (
            f"{start.longitude:.6f},{start.latitude:.6f};"
            f"{finish.longitude:.6f},{finish.latitude:.6f}"
        )
        url = f"{self.osrm_url}/route/v1/driving/{coordinates}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": self.user_agent},
        ) as client:
            data = await get_json(
                client,
                url,
                params={"overview": "false", "steps": "false", "alternatives": "false"},
                service_name="маршрутизації",
            )
        try:
            if data.get("code") != "Ok" or not data.get("routes"):
                raise KeyError("route")
            metres = Decimal(str(data["routes"][0]["distance"]))
        except (AttributeError, KeyError, IndexError, InvalidOperation, TypeError) as exc:
            raise ExternalServiceError("Не вдалося побудувати автомобільний маршрут.") from exc
        return (metres / Decimal("1000")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


NBU = NBUClient()
ROUTER = GeoRouter()


def allowed_user_ids() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    try:
        return {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated Telegram IDs") from exc


ALLOWED_USERS = allowed_user_ids()


async def ensure_access(update: Update) -> bool:
    user = update.effective_user
    if not ALLOWED_USERS or (user and user.id in ALLOWED_USERS):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Цей бот приватний. Ваш ID: " + str(user.id))
    return False


def calculation_message(
    calc: Calculation,
    customs: GeoPoint | None = None,
    customs_input: str | None = None,
) -> str:
    total_km = calc.first_km + calc.second_km
    route_parts = [calc.request.origin, calc.request.border]
    if customs_input:
        route_parts.append(customs_input)
    route_parts.append(calc.request.destination)
    route_text = " → ".join(html.escape(part) for part in route_parts)

    verification = [
        f"• Завантаження: {html.escape(calc.origin.label)}",
        f"• Перехід: {html.escape(calc.border.label)}",
    ]
    if customs is not None:
        verification.append(f"• Розмитнення: {html.escape(customs.label)}")
    verification.append(f"• Вивантаження: {html.escape(calc.destination.label)}")

    return (
        "<b>🚛 Розподіл ставки за кілометражем</b>\n"
        f"<b>Маршрут:</b> {route_text}\n"
        f"<b>Ставка:</b> {money(calc.request.amount)} {calc.request.currency} = "
        f"{money(calc.hryvnia.total)} грн\n"
        f"<b>1. До кордону —</b> {money(calc.hryvnia.first)} грн\n"
        f"<b>2. Після кордону —</b> {money(calc.hryvnia.second)} грн\n\n"
        f"<b>Разом:</b> {number(total_km)} км\n"
        f"<b>Курс НБУ:</b> 1 {calc.request.currency} = "
        f"{number(calc.rate, 4)} грн ({html.escape(calc.rate_date)})\n\n"
        "<b>Перевірка знайдених точок:</b>\n"
        + "\n".join(verification)
    )


def manual_message(
    amount: Decimal,
    currency: str,
    rate: Decimal,
    rate_date: str,
    first_km: Decimal,
    second_km: Decimal,
) -> str:
    foreign = split_total(amount, first_km, second_km)
    total_uah = (amount * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
    hryvnia = split_total(total_uah, first_km, second_km)
    total_km = first_km + second_km
    return (
        "<b>🚛 Розподіл за заданим кілометражем</b>\n\n"
        f"<b>Ставка:</b> {money(amount)} {currency}\n"
        f"<b>Курс НБУ:</b> 1 {currency} = {number(rate, 4)} грн ({html.escape(rate_date)})\n\n"
        f"<b>1. До кордону — {number(first_km)} км</b>\n"
        f"{money(foreign.first)} {currency}\n{money(hryvnia.first)} грн\n\n"
        f"<b>2. Після кордону — {number(second_km)} км</b>\n"
        f"{money(foreign.second)} {currency}\n{money(hryvnia.second)} грн\n\n"
        f"<b>Разом:</b> {number(total_km)} км · {money(foreign.total)} {currency} · "
        f"{money(hryvnia.total)} грн"
    )


FORM_LOADING, FORM_BORDER, FORM_CUSTOMS, FORM_DESTINATION, FORM_AMOUNT = range(5)
FORM_KEY = "route_split_form"
START_BUTTON = "🚛 Новий розрахунок"
START_BUTTON_PATTERN = re.compile(
    r"^(?:🚛\\s*)?(?:новий розрахунок|старт|стоп)$",
    flags=re.IGNORECASE,
)
START_KEYBOARD = ReplyKeyboardMarkup(
    [[START_BUTTON]],
    resize_keyboard=True,
)


async def start_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await ensure_access(update) or not update.effective_message:
        return ConversationHandler.END
    context.user_data[FORM_KEY] = {}
    await update.effective_message.reply_text(
        "🚛 Новий розрахунок\n\n1/5. Вкажіть місце завантаження:",
        reply_markup=START_KEYBOARD,
    )
    return FORM_LOADING


async def collect_loading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[FORM_KEY]["origin"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text(
        "2/5. Вкажіть перехід, наприклад: Ягодин або Ужгород:"
    )
    return FORM_BORDER


async def collect_border(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[FORM_KEY]["border"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text(
        "3/5. Вкажіть місто або адресу розмитнення:"
    )
    return FORM_CUSTOMS


async def collect_customs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[FORM_KEY]["customs"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text(
        "4/5. Вкажіть місце вивантаження:"
    )
    return FORM_DESTINATION


async def collect_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[FORM_KEY]["destination"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text(
        "5/5. Вкажіть ставку, наприклад: 4500е або 3500дол:"
    )
    return FORM_AMOUNT


async def calculate_request(
    message,
    request: TripRequest,
    customs_query: str | None = None,
) -> None:
    status = await message.reply_text("⏳ Отримую курс НБУ та шукаю точки маршруту…")
    try:
        rate, rate_date = await NBU.rate(request.currency)
        origin = await ROUTER.resolve_place(request.origin, "завантаження")
        border = await ROUTER.resolve_border(request.border)
        customs = None
        if customs_query:
            customs = await ROUTER.resolve_place(customs_query, "розмитнення")
        destination = await ROUTER.resolve_place(request.destination, "вивантаження")

        await status.edit_text("⏳ Будую автомобільний маршрут…")
        first_km = await ROUTER.distance_km(origin, border)
        if customs is not None:
            to_customs_km, to_destination_km = await asyncio.gather(
                ROUTER.distance_km(border, customs),
                ROUTER.distance_km(customs, destination),
            )
            second_km = to_customs_km + to_destination_km
        else:
            second_km = await ROUTER.distance_km(border, destination)

        foreign = split_total(request.amount, first_km, second_km)
        total_uah = (request.amount * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
        hryvnia = split_total(total_uah, first_km, second_km)
        calc = Calculation(
            request=request,
            rate=rate,
            rate_date=rate_date,
            origin=origin,
            border=border,
            destination=destination,
            first_km=first_km,
            second_km=second_km,
            foreign=foreign,
            hryvnia=hryvnia,
        )
        await status.edit_text(
            calculation_message(calc, customs=customs, customs_input=customs_query),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except (UserInputError, ExternalServiceError) as exc:
        await status.edit_text(f"⚠️ {exc}")

    await message.reply_text(
        "Щоб порахувати наступну ставку, натисніть кнопку:",
        reply_markup=START_KEYBOARD,
    )


async def collect_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    try:
        amount, currency = parse_amount_currency(message.text or "")
    except UserInputError as exc:
        await message.reply_text(
            f"⚠️ {exc}\nНапишіть, наприклад: 4500е або 3500дол"
        )
        return FORM_AMOUNT

    form = context.user_data.get(FORM_KEY, {})
    required = ("origin", "border", "customs", "destination")
    if any(not form.get(field) for field in required):
        context.user_data.pop(FORM_KEY, None)
        await message.reply_text("Дані анкети втрачено. Натисніть /start і заповніть ще раз.")
        return ConversationHandler.END

    request = TripRequest(
        amount=amount,
        currency=currency,
        origin=form["origin"],
        border=form["border"],
        destination=form["destination"],
    )
    await calculate_request(message, request, customs_query=form["customs"])
    context.user_data.pop(FORM_KEY, None)
    return ConversationHandler.END


async def cancel_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(FORM_KEY, None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Заповнення скасовано. Для нового розрахунку натисніть /start."
        )
    return ConversationHandler.END


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if await ensure_access(update) and update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message and update.effective_user:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def show_rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await ensure_access(update) or not update.effective_message:
        return
    status = await update.effective_message.reply_text("Отримую курс НБУ…")
    try:
        (usd, usd_date), (eur, eur_date) = await asyncio.gather(NBU.rate("USD"), NBU.rate("EUR"))
        await status.edit_text(
            "<b>Офіційний курс НБУ</b>\n\n"
            f"1 USD = {number(usd, 4)} грн ({html.escape(usd_date)})\n"
            f"1 EUR = {number(eur, 4)} грн ({html.escape(eur_date)})",
            parse_mode=ParseMode.HTML,
        )
    except (UserInputError, ExternalServiceError) as exc:
        await status.edit_text(f"⚠️ {exc}")


async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await ensure_access(update) or not update.effective_message:
        return
    try:
        request = parse_trip_request(update.effective_message.text or "")
    except UserInputError as exc:
        await update.effective_message.reply_text(
            f"⚠️ {exc}\n\nПриклад:\n<code>Гданськ - Ягодин - Київ 3500 дол</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await calculate_request(update.effective_message, request)


async def calculate_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await ensure_access(update) or not update.effective_message:
        return
    try:
        amount, currency, first_km, second_km = parse_manual_request(
            update.effective_message.text or ""
        )
    except UserInputError as exc:
        await update.effective_message.reply_text(
            f"⚠️ {exc}\n\nПриклад:\n<code>/manual 3400 USD | 720 | 510</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    status = await update.effective_message.reply_text("Отримую курс НБУ…")
    try:
        rate, rate_date = await NBU.rate(currency)
        await status.edit_text(
            manual_message(amount, currency, rate, rate_date, first_km, second_km),
            parse_mode=ParseMode.HTML,
        )
    except (UserInputError, ExternalServiceError) as exc:
        await status.edit_text(f"⚠️ {exc}")

    await update.effective_message.reply_text(
        "Щоб порахувати наступну ставку, натисніть кнопку:",
        reply_markup=START_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Сталася непередбачена помилка. Спробуйте ще раз або перевірте логи Railway."
            )
        except Exception:  # pragma: no cover - Telegram itself may be unavailable
            LOGGER.exception("Could not notify user about the error")


def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    application = Application.builder().token(token).build()
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler(["start", "new", "stop"], start_form),
            MessageHandler(filters.Regex(START_BUTTON_PATTERN), start_form),
        ],
        states={
            FORM_LOADING: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_loading)],
            FORM_BORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_border)],
            FORM_CUSTOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_customs)],
            FORM_DESTINATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_destination)
            ],
            FORM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel_form)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("rate", show_rates))
    application.add_handler(CommandHandler("calc", calculate))
    application.add_handler(CommandHandler("manual", calculate_manual))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, calculate))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info("Starting NBU route split bot")
    build_application().run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
