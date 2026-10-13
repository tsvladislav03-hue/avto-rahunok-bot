import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from bot import (
    UserInputError,
    NBUClient,
    border_queries,
    known_border_point,
    money,
    parse_amount_currency,
    parse_manual_request,
    parse_trip_request,
    split_total,
)


class ParsingTests(unittest.TestCase):
    def test_trip_command(self):
        request = parse_trip_request("/calc 3400 USD | Гданськ | Ягодин | Київ")
        self.assertEqual(request.amount, Decimal("3400"))
        self.assertEqual(request.currency, "USD")
        self.assertEqual(request.origin, "Гданськ")
        self.assertEqual(request.border, "Ягодин")
        self.assertEqual(request.destination, "Київ")

    def test_euro_and_decimal_comma(self):
        request = parse_trip_request("2800,50 € | Rotterdam | Ужгород | Одеса")
        self.assertEqual(request.amount, Decimal("2800.50"))
        self.assertEqual(request.currency, "EUR")

    def test_thousands_separator(self):
        amount, currency = parse_amount_currency("$3,400")
        self.assertEqual(amount, Decimal("3400"))
        self.assertEqual(currency, "USD")

    def test_manual_command(self):
        amount, currency, first, second = parse_manual_request(
            "/manual 3400 дол | 720 км | 510 км"
        )
        self.assertEqual((amount, currency), (Decimal("3400"), "USD"))
        self.assertEqual((first, second), (Decimal("720"), Decimal("510")))

    def test_bad_format(self):
        with self.assertRaises(UserInputError):
            parse_trip_request("3400 USD Гданськ Київ")

    def test_border_alias(self):
        self.assertIn("Dorohusk", border_queries("Перехід Ягодин")[0])
        point = known_border_point("Перехід Ягодин–Дорогуськ")
        self.assertIsNotNone(point)
        self.assertEqual(point.label, "МАПП Ягодин — Дорогуськ")

    def test_uzhhorod_checkpoint_is_not_city_centre(self):
        point = known_border_point("Ужгород")
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.latitude, 48.6535970)


class CalculationTests(unittest.TestCase):
    def test_split_keeps_exact_total(self):
        result = split_total(Decimal("3400"), Decimal("720"), Decimal("510"))
        self.assertEqual(result.first + result.second, Decimal("3400.00"))
        self.assertEqual(result.first, Decimal("1990.24"))
        self.assertEqual(result.second, Decimal("1409.76"))

    def test_uah_split_keeps_exact_total(self):
        total_uah = (Decimal("3400") * Decimal("43.4997")).quantize(Decimal("0.01"))
        result = split_total(total_uah, Decimal("720"), Decimal("510"))
        self.assertEqual(result.first + result.second, total_uah)

    def test_money_format(self):
        self.assertEqual(money(Decimal("147898.98")), "147 898,98")


class NBUClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_rate_response_is_parsed(self):
        payload = [{"cc": "USD", "rate": 43.4997, "exchangedate": "13.10.2026"}]
        with patch("bot.get_json", new=AsyncMock(return_value=payload)):
            rate, rate_date = await NBUClient().rate("USD")
        self.assertEqual(rate, Decimal("43.4997"))
        self.assertEqual(rate_date, "13.10.2026")


if __name__ == "__main__":
    unittest.main()
