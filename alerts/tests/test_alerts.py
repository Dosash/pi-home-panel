import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alerts  # noqa: E402
import sms_settings  # noqa: E402
from alerts import Pause, Thermo, deg, duration, on_start, power_message, send_next, sync_thermos  # noqa: E402
from modem import ModemError  # noqa: E402


def ts(day, hh, mm):
    return datetime(2026, 10, day, hh, mm).timestamp()


class Power(unittest.TestCase):
    def test_first_start_is_quiet(self):
        self.assertEqual(on_start({}, "b1", 100.0, True)["pending"], [])

    def test_service_restart_in_same_boot(self):
        state = {"boot": "b1", "clean": True, "alive": 50.0, "pending": []}
        self.assertEqual(on_start(state, "b1", 100.0, True)["pending"], [])

    def test_clean_shutdown(self):
        state = {"boot": "b1", "clean": True, "alive": 50.0, "alive_exact": True}
        self.assertEqual(on_start(state, "b2", 100.0, False)["pending"], [])

    def test_power_loss(self):
        state = {"boot": "b1", "clean": False, "alive": 50.0, "alive_exact": True}
        new = on_start(state, "b2", 100.0, False)
        self.assertEqual(new["pending"], [{"boot": "b2", "off": 50.0, "off_exact": True}])
        self.assertFalse(new["clean"])
        self.assertEqual(new["outbox"], [])

    def test_losses_pile_up_until_sent(self):
        state = on_start({"boot": "b1", "alive": 50.0, "alive_exact": True}, "b2", 60.0, False)
        state = on_start(state, "b3", 70.0, False)
        self.assertEqual([p["boot"] for p in state["pending"]], ["b2", "b3"])
        self.assertEqual(state["pending"][1]["off"], 60.0)

    def test_power_on_noted_for_this_boot_only(self):
        state = {"pending": [{"boot": "b1", "off": 1.0, "off_exact": True},
                             {"boot": "b2", "off": 2.0, "off_exact": True}]}
        alerts.note_power_on(state, "b2", 1000.0, 40.0)
        self.assertNotIn("on", state["pending"][0])
        self.assertEqual(state["pending"][1]["on"], 960.0)

    def test_message_fits_one_sms(self):
        text = power_message([{"off": ts(7, 14, 32), "off_exact": True, "on": ts(7, 15, 10)}])
        self.assertEqual(text, "Pi: пропадало питание 07.10 14:32–15:10 (38 мин). Работает.")
        self.assertLessEqual(len(text), 70)

    def test_message_over_midnight(self):
        text = power_message([{"off": ts(6, 23, 50), "off_exact": True, "on": ts(7, 1, 5)}])
        self.assertEqual(text, "Pi: пропадало питание 06.10 23:50–07.10 01:05 (1 ч 15 мин). Работает.")

    def test_message_without_power_on_time(self):
        text = power_message([{"off": ts(7, 14, 32), "off_exact": True}])
        self.assertEqual(text, "Pi: пропадало питание с 07.10 14:32. Работает.")

    def test_message_inexact_clock(self):
        text = power_message([{"off": ts(7, 14, 32), "off_exact": False, "on": ts(7, 15, 10)}])
        self.assertEqual(text, "Pi: пропадало питание ≈07.10 14:32–15:10. Работает.")

    def test_message_several(self):
        p = {"off": ts(7, 14, 32), "off_exact": True, "on": ts(7, 15, 10)}
        self.assertTrue(power_message([p] * 3).startswith("Pi: питание пропадало 3 раза, последний 07.10"))
        self.assertIn(" 5 раз,", power_message([p] * 5))


class Text(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(duration(20), "1 мин")
        self.assertEqual(duration(38 * 60), "38 мин")
        self.assertEqual(duration(2 * 3600), "2 ч")
        self.assertEqual(duration(26 * 3600 + 60), "1 д 2 ч")

    def test_deg(self):
        self.assertEqual(deg(-1.5), "−1,5 °C")
        self.assertEqual(deg(2.0), "+2 °C")
        self.assertEqual(deg(0), "0 °C")
        self.assertEqual(deg(-0.04), "0 °C")


class Temperature(unittest.TestCase):
    def setUp(self):
        self.t = Thermo("Подвал", "A4:C1:38:00:00:01", 0.0, 7200, now=0)

    def test_matches_name_or_address(self):
        self.assertTrue(self.t.matches(None, "a4:c1:38:00:00:01"))
        self.assertFalse(self.t.matches("ATC_000001", "A4:C1:38:00:00:02"))

    def test_cold_after_hold_once(self):
        self.t.reading(-0.5, 10)
        self.assertEqual(self.t.check(10), [])  # один замер — ещё не повод
        self.t.reading(-1.5, 200)
        self.assertEqual(self.t.check(10 + alerts.TEMP_HOLD), ["Pi: холодно — Подвал −1,5 °C (порог 0 °C)."])
        self.t.reading(-2.0, 400)
        self.assertEqual(self.t.check(400), [])  # уже сообщили

    def test_short_dip_is_ignored(self):
        self.t.reading(-0.5, 10)
        self.t.reading(0.5, 100)
        self.assertEqual(self.t.check(10 + alerts.TEMP_HOLD), [])

    def test_warm_again_with_hysteresis(self):
        self.t.reading(-1.0, 0)
        self.t.check(alerts.TEMP_HOLD)
        self.t.reading(0.5, 400)
        self.assertEqual(self.t.check(400), [])  # чуть выше порога — ещё не отпустило
        self.t.reading(1.2, 500)
        self.assertEqual(self.t.check(500, ", 07.10 09:40"), ["Pi: Подвал снова +1,2 °C, 07.10 09:40."])

    def test_silent_sensor(self):
        self.t.reading(3.0, 0)
        self.assertEqual(self.t.check(7200), ["Pi: датчик Подвал молчит 2 ч, последний раз +3 °C."])
        self.assertEqual(self.t.check(9000), [])
        self.t.reading(2.5, 9100)
        self.assertEqual(self.t.check(9100), ["Pi: датчик Подвал снова на связи, +2,5 °C."])

    def test_never_heard(self):
        self.assertEqual(self.t.check(7200), ["Pi: датчик Подвал молчит 2 ч."])

    def test_silence_alert_off(self):
        self.t.stale = 0
        self.assertEqual(self.t.check(10 ** 6), [])

    def test_settings_change_keeps_what_was_reported(self):
        s = sms_settings.validate({"temperature": {"sensors": [{"id": "A4:C1:38:00:00:01", "label": "Подвал"}]}})
        [th] = sync_thermos([], s, now=0)
        th.reading(-3.0, 0)
        th.check(alerts.TEMP_HOLD)  # «холодно» уже ушло
        s["temperature"]["sensors"][0]["label"] = "Погреб"
        [same] = sync_thermos([th], s, now=500)
        self.assertIs(same, th)
        self.assertEqual(same.label, "Погреб")
        self.assertEqual(same.check(600), [])
        s["temperature"]["enabled"] = False
        self.assertEqual(sync_thermos([th], s, now=700), [])


class Motion(unittest.TestCase):
    def test_pause_between_sms(self):
        p = Pause()
        self.assertTrue(p.ready(0, 1800))
        self.assertFalse(p.ready(600, 1800))
        self.assertTrue(p.ready(1800, 1800))


class Settings(unittest.TestCase):
    def test_defaults(self):
        s = sms_settings.validate({})
        self.assertEqual(s["phones"], [])
        self.assertTrue(s["power"]["enabled"])
        self.assertFalse(s["motion"]["enabled"])
        self.assertEqual(s["temperature"]["min"], 0.0)

    def test_phones_from_text(self):
        s = sms_settings.validate({"phones": "+7 (999) 000-00-01; 8 999 000-00-02, +79990000001"})
        self.assertEqual(s["phones"], ["+79990000001", "89990000002"])

    def test_bad_input_explained(self):
        for data, text in [({"phones": "112"}, "номер"),
                           ({"temperature": {"min": "холодно"}}, "число"),
                           ({"temperature": {"min": 99}}, "от -40 до 40"),
                           ({"motion": {"pause": 0}}, "Пауза")]:
            with self.assertRaises(ValueError) as e:
                sms_settings.validate(data)
            self.assertIn(text, str(e.exception))

    def test_numbers_with_comma_and_sensors(self):
        s = sms_settings.validate({"temperature": {"min": "−2,5", "sensors": [
            {"id": "0x00158d0001234567", "label": ""}, {"id": "0x00158D0001234567"}, {"id": ""}]}})
        self.assertEqual(s["temperature"]["min"], -2.5)
        self.assertEqual(s["temperature"]["sensors"], [{"id": "0x00158D0001234567", "label": "0x00158D0001234567"}])

    def test_load_missing_file(self):
        self.assertEqual(sms_settings.load(Path("/nonexistent/settings.json")), sms_settings.DEFAULTS)


class FakeModem:
    def __init__(self, ok):
        self.ok, self.calls = ok, []

    def send_sms(self, phones, text):
        self.calls.append(phones)
        ok = [p for p in phones if p in self.ok]
        return ok, [p for p in phones if p not in self.ok]


class Sending(unittest.TestCase):
    def test_retry_only_failed_numbers(self):
        state = {"outbox": [{"text": "x", "sent_to": []}]}
        phones = ["+79990000001", "89990000002"]
        modem = FakeModem({"+79990000001"})
        with self.assertRaises(ModemError):
            send_next(modem, state, phones)
        modem.ok = {"89990000002"}
        self.assertTrue(send_next(modem, state, phones))
        self.assertEqual(modem.calls, [phones, ["89990000002"]])
        self.assertEqual(state["outbox"], [])
        self.assertEqual(state["log"][-1]["text"], "x")

    def test_number_formats(self):
        self.assertTrue(alerts.same_number("+7 (999) 000-00-01", "89990000001"))


if __name__ == "__main__":
    unittest.main()
