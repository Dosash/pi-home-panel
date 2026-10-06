"""Тесты ble_sensors: векторы из спецификаций и тестов эталонных библиотек."""

import random
import struct
import unittest

from ble_sensors import (
    UUID_ATC,
    UUID_BTHOME,
    UUID_MIBEACON,
    UUID_QINGPING,
    UUID_SWITCHBOT,
    UUID_SWITCHBOT_OLD,
    decode,
)

H = bytes.fromhex


def sd(uuid, hexdata, name=None):
    return decode(name, {uuid: H(hexdata)}, {})


class BTHomeTest(unittest.TestCase):
    # https://bthome.io/format/ — пример из раздела «BTHome Data format»: D2FC 40 02C409 03BF13
    def test_spec_example(self):
        r = sd(UUID_BTHOME, "4002c40903bf13", "DIY-sensor")
        self.assertEqual(r["format"], "bthome-v2")
        self.assertFalse(r["encrypted"])
        self.assertEqual((r["brand"], r["model"]), ("BTHome", "BTHome sensor"))
        self.assertEqual(r["values"], {"temperature": 25.0, "humidity": 50.55})

    # https://bthome.io/format/ — колонка «Example» таблиц Sensor data / Binary / Events
    def test_spec_object_examples(self):
        cases = [
            ("0161", {"battery": 97}),
            ("02CA09", {"temperature": 25.06}),
            ("03BF13", {"humidity": 50.55}),
            ("04138A01", {"pressure": 1008.83}),
            ("05138A14", {"illuminance": 13460.67}),
            ("08CA06", {"dew_point": 17.38}),
            ("0C020C", {"voltage": 3.074}),
            ("0D120C", {"pm25": 3090}),
            ("0E021C", {"pm10": 7170}),
            ("12E204", {"co2": 1250}),
            ("14020C", {"moisture": 30.74}),
            ("2E23", {"humidity": 35}),
            ("2F23", {"moisture": 35}),
            ("451101", {"temperature": 27.3}),
            ("4A020C", {"voltage": 307.4}),
            ("56E803", {"conductivity": 1000}),
            ("57EA", {"temperature": -22}),
            ("58EA", {"temperature": -7.7}),
            ("1100", {"opening": False}),
            ("2D01", {"opening": True}),
            ("2100", {"motion": False}),
            ("3A04", {"button": "long_press"}),
            ("3A80", {"button": "hold_press"}),
            ("0009", {"packet_id": 9}),
        ]
        for obj, expected in cases:
            with self.subTest(obj=obj):
                self.assertEqual(sd(UUID_BTHOME, "40" + obj)["values"], expected)

    # https://bthome.io/format/ — примеры неотображаемых объектов (energy, power, command, text, raw,
    # device info), склеенные перед conductivity: проверяем, что парсер их корректно перешагивает
    def test_skips_unmapped_objects(self):
        payload = "40" + "0A138A14" "0B021B00" "3B010305" "530C48656C6C6F20576F726C6421" "540C48656C6C6F20576F726C6421" "F00100" "F100010204" "56E803"
        self.assertEqual(sd(UUID_BTHOME, payload)["values"], {"conductivity": 1000})

    # https://github.com/Bluetooth-Devices/bthome-ble/blob/main/tests/test_parser_v2.py
    # test_bthome_packet_id_temperature_humidity_battery
    def test_packet_id_temp_hum_battery(self):
        r = sd(UUID_BTHOME, "40000901" "5d025d0903b718", "ATC_8D18B2")
        self.assertEqual((r["brand"], r["model"]), ("Xiaomi", "ATC"))
        self.assertEqual(r["values"], {"packet_id": 9, "battery": 93, "temperature": 23.97, "humidity": 63.27})

    # там же: test_bthome_shelly_button — b"@\x00R\x01d:\x01"
    def test_shelly_button(self):
        r = sd(UUID_BTHOME, "4000520164" "3a01")
        self.assertEqual(r["values"], {"packet_id": 82, "battery": 100, "button": "press"})

    # там же: test_bthome_triple_temperature_double_humidity_battery
    def test_repeated_objects_get_suffix(self):
        r = sd(UUID_BTHOME, "4002ca0902cf0902cf0803b71803b717015d")
        self.assertEqual(
            r["values"],
            {"temperature": 25.06, "temperature_2": 25.11, "temperature_3": 22.55, "humidity": 63.27, "humidity_2": 60.71, "battery": 93},
        )

    # https://bthome.io/format/ — «3A003A01»: первая кнопка без события, вторая — press
    def test_button_none_event_keeps_position(self):
        self.assertEqual(sd(UUID_BTHOME, "403a003a01")["values"], {"button_2": "press"})

    # bthome-ble tests: test_bthome_battery_wrong_object_id_humidity — на неизвестном id 0xFE разбор останавливается
    def test_unknown_object_stops(self):
        self.assertEqual(sd(UUID_BTHOME, "40015dfe5d0903b718")["values"], {"battery": 93})
        # test_bthome_wrong_object_id
        self.assertEqual(sd(UUID_BTHOME, "40feca09")["values"], {})

    # bthome-ble tests: test_bthome_with_mac — бит 1 device info, перед объектами 6 байт MAC
    def test_mac_included(self):
        self.assertEqual(sd(UUID_BTHOME, "42b2188d38c1a404138a01")["values"], {"pressure": 1008.83})

    # bthome-ble tests: test_bindkey_correct (Shelly BLU H&T, шифрованный пакет)
    def test_encrypted(self):
        r = sd(UUID_BTHOME, "41a47266c95f730011223378237214", "SBHT-003C")
        self.assertEqual((r["brand"], r["model"], r["encrypted"], r["values"]), ("Shelly", "BLU H&T", True, {}))
        # test_encryption_key_needed
        r = sd(UUID_BTHOME, "41d2fcefe52eb212d600112233bc38c966", "ATC_8D18B2")
        self.assertTrue(r["encrypted"])
        self.assertEqual(r["values"], {})

    # bthome-ble tests: test_incorrect_bthome_version — b"\x00"; биты версии должны быть 010
    def test_wrong_version(self):
        self.assertIsNone(sd(UUID_BTHOME, "00"))
        self.assertIsNone(sd(UUID_BTHOME, "2002c409"))  # версия 1 под UUID v2
        self.assertIsNone(sd(UUID_BTHOME, ""))

    # bthome-ble tests: test_bthome_invalid_object_payload_data_length, test_truncated_v2_object_length_byte
    def test_truncated(self):
        self.assertEqual(sd(UUID_BTHOME, "4002ca0903bf")["values"], {"temperature": 25.06})
        for tail in ("53", "3b", "54", "530c4865", "3b01"):
            with self.subTest(tail=tail):
                self.assertEqual(sd(UUID_BTHOME, "40" + tail)["values"], {})
        self.assertEqual(sd(UUID_BTHOME, "40")["values"], {})
        self.assertEqual(sd(UUID_BTHOME, "42b2188d")["values"], {})  # MAC оборван


class AtcPvvxTest(unittest.TestCase):
    # https://github.com/custom-components/ble_monitor/blob/master/custom_components/ble_monitor/test/test_atc_parser.py
    # test_atc_custom (AD 12161a18 …, service data без UUID)
    def test_pvvx_custom(self):
        r = sd(UUID_ATC, "f4830238c1a4a9066911b60b58f70d")
        self.assertEqual((r["brand"], r["model"], r["format"], r["encrypted"]), ("Xiaomi", "ATC", "pvvx", False))
        self.assertEqual(r["values"], {"temperature": 17.05, "humidity": 44.57, "voltage": 2.998, "battery": 88})

    # там же: test_atc_custom_v2_9
    def test_pvvx_custom_v29(self):
        r = sd(UUID_ATC, "b2188d38c1a42b089011f70a43200f")
        self.assertEqual(r["values"], {"temperature": 20.91, "humidity": 44.96, "voltage": 2.807, "battery": 67})

    # там же: test_atc_atc1441
    def test_atc1441(self):
        r = sd(UUID_ATC, "a4c1380283f400a22f5f0bf819")
        self.assertEqual(r["format"], "atc1441")
        self.assertEqual(r["values"], {"temperature": 16.2, "humidity": 47, "voltage": 3.064, "battery": 95})

    # там же: test_atc_atc1441_ext
    def test_atc1441_ext(self):
        r = sd(UUID_ATC, "a4c138bc7c4e0102284f0b6720")
        self.assertEqual(r["values"], {"temperature": 25.8, "humidity": 40, "voltage": 2.919, "battery": 79})

    # там же: test_atc_custom_encrypted (AD 0e161a18 …)
    def test_encrypted(self):
        r = sd(UUID_ATC, "11d603fbfa7b6dfb1e26fd")
        self.assertEqual((r["format"], r["encrypted"], r["values"]), ("pvvx", True, {}))

    def test_truncated(self):
        full = "f4830238c1a4a9066911b60b58f70d"
        self.assertIsNone(sd(UUID_ATC, full[:-2]))  # 14 байт — не наш формат
        self.assertIsNone(sd(UUID_ATC, ""))
        self.assertIsNone(sd(UUID_ATC, "a4c1380283f400a22f5f0bf8"))  # atc1441 без счётчика


class MiBeaconTest(unittest.TestCase):
    # Все векторы — https://github.com/Bluetooth-Devices/xiaomi-ble/blob/main/tests/test_parser.py

    # test_Xiaomi_LYWSDCGQ: объекты 0x100D (temp+hum) и 0x100A (батарея)
    def test_lywsdcgq(self):
        r = sd(UUID_MIBEACON, "5020aa01a3bf2e3b342d580d1004b40095020a10013b")
        self.assertEqual((r["brand"], r["model"], r["format"], r["encrypted"]), ("Xiaomi", "LYWSDCGQ", "mibeacon-v2", False))
        self.assertEqual(r["values"], {"temperature": 18.0, "humidity": 66.1, "battery": 59})

    # test_Xiaomi_LYWSD03MMC: влажность у этой модели — целые проценты
    def test_lywsd03mmc_unencrypted(self):
        r = sd(UUID_MIBEACON, "50305b05034c94b438c1a40d10041001ea01")
        self.assertEqual((r["model"], r["format"]), ("LYWSD03MMC", "mibeacon-v3"))
        self.assertEqual(r["values"], {"temperature": 27.2, "humidity": 49})

    # test_Xiaomi_ESM787_temperature_humidity: v5 без шифрования, бренд Yanmi
    def test_esm787(self):
        r = sd(UUID_MIBEACON, "5059db780df4f80238c1a40d1004eb00e701")
        self.assertEqual((r["brand"], r["model"], r["format"]), ("Yanmi", "ESM787", "mibeacon-v5"))
        self.assertEqual(r["values"], {"temperature": 23.5, "humidity": 48.7})

    # test_Xiaomi_HHCCJCY01 (с capability-байтом) и test_Xiaomi_HHCCJCY01_all_values
    def test_flower_care(self):
        cases = [
            ("7120980012f34f6b8d7cc40d041002c400", {"temperature": 19.6}),
            ("71209800667a3e6a8d7cc40d071003000000", {"illuminance": 0}),
            ("71209800687a3e6a8d7cc40d0910025702", {"conductivity": 599}),
            ("71209800477a3e6a8d7cc40d08100140", {"moisture": 64}),
            ("71209800697a3e6a8d7cc40d041002f400", {"temperature": 24.4}),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                r = sd(UUID_MIBEACON, data)
                self.assertEqual(r["model"], "HHCCJCY01")
                self.assertEqual(r["values"], expected)

    # test_Xiaomi_CGH1 (0x1019 = 0 → открыто) и test_Xiaomi_CGPR1 (0x000F → движение + люксы)
    def test_door_and_motion(self):
        self.assertEqual(sd(UUID_MIBEACON, "5040d60301d6030a38c1a419100100")["values"], {"opening": True})
        self.assertEqual(sd(UUID_MIBEACON, "5040830a0183000a38c1a40f0003640000")["values"], {"motion": True, "illuminance": 100})

    # Синтетический пакет по раскладке obj4c01/obj4c02/obj4c03 из xiaomi-ble parser.py:
    # temp float32 LE, humidity uint8, battery uint8 (MJWSD06MMC, v5, без шифрования)
    def test_4cxx_objects(self):
        frame = H("5050b55501") + H("112233445566") + H("014c04") + struct.pack("<f", 21.53) + H("024c0137") + H("034c0164")
        r = decode(None, {UUID_MIBEACON: frame}, {})
        self.assertEqual(r["model"], "MJWSD06MMC")
        self.assertEqual(r["values"], {"temperature": 21.53, "humidity": 55, "battery": 100})

    # test_Xiaomi_LYWSD03MMC_encrypted и test_Xiaomi_CGG1 (v5, бит шифрования)
    def test_encrypted(self):
        r = sd(UUID_MIBEACON, "58585b0550f4830238c1a495ef58763c26000097e2abb5")
        self.assertEqual((r["model"], r["format"], r["encrypted"], r["values"]), ("LYWSD03MMC", "mibeacon-v5", True, {}))
        r = sd(UUID_MIBEACON, "5858480b685f12342d585a0b1841e2aa000e00a4964fb5")
        self.assertEqual((r["model"], r["encrypted"]), ("CGG1", True))

    # test_blank_advertisements_then_encrypted: маяк без объектов
    def test_no_objects(self):
        r = sd(UUID_MIBEACON, "30585b0502483cd438c1a408")
        self.assertEqual((r["model"], r["encrypted"], r["values"]), ("LYWSD03MMC", False, {}))

    # test_Xiaomi_unknown_device_logs_product_id: product id 0xFFFF
    def test_unknown_product(self):
        self.assertIsNone(sd(UUID_MIBEACON, "5020ffffa3bf2e3b342d580d1004b40095020a10013b"))

    def test_mesh_and_old_versions_rejected(self):
        self.assertIsNone(sd(UUID_MIBEACON, "d020aa01a3bf2e3b342d580d1004b400950201"))  # бит mesh
        self.assertIsNone(sd(UUID_MIBEACON, "5010aa01a3bf2e3b342d580d1004b40095020a10013b"))  # версия 1

    def test_truncated(self):
        full = "5020aa01a3bf2e3b342d580d1004b40095020a10013b"
        # оборван объект батареи — температура и влажность остаются
        self.assertEqual(sd(UUID_MIBEACON, full[:-2])["values"], {"temperature": 18.0, "humidity": 66.1})
        self.assertIsNone(sd(UUID_MIBEACON, full[:8]))  # нет frame counter
        self.assertIsNone(sd(UUID_MIBEACON, full[:16]))  # MAC оборван
        self.assertEqual(sd(UUID_MIBEACON, full[:22])["values"], {})  # заголовок цел, объектов нет
        self.assertIsNone(sd(UUID_MIBEACON, "7120980012f34f6b8d7c"))  # без capability-байта


class QingpingTest(unittest.TestCase):
    # Все векторы — https://github.com/Bluetooth-Devices/qingping-ble/blob/main/tests/test_parser.py

    # QINGPING_TEMP_RH_M_CGG1
    def test_cgg1(self):
        r = sd(UUID_QINGPING, "0816a72514342d580104d800bb01020164")
        self.assertEqual((r["brand"], r["model"], r["format"], r["encrypted"]), ("Qingping", "CGG1", "qingping", False))
        self.assertEqual(r["values"], {"temperature": 21.6, "humidity": 44.3, "battery": 100})

    # QINGPING_CGP22C_REAL
    def test_cgp22c_co2(self):
        r = sd(UUID_QINGPING, "0a5d931d86342d5801041701ce010201641302b302")
        self.assertEqual(r["model"], "CGP22C")
        self.assertEqual(r["values"], {"temperature": 27.9, "humidity": 46.2, "battery": 100, "co2": 691})

    # QINGPING_DOOR_WINDOW: 0x04 = 1 → закрыто
    def test_door(self):
        self.assertEqual(sd(UUID_QINGPING, "c8044d3a40342d580401010f01ef")["values"], {"opening": False})

    def test_unknown_and_truncated(self):
        self.assertIsNone(sd(UUID_QINGPING, "08ffa72514342d580104d800bb01"))
        self.assertIsNone(sd(UUID_QINGPING, "08"))
        self.assertEqual(sd(UUID_QINGPING, "0816")["values"], {})  # ALARM_CLOCK-подобный короткий пакет
        self.assertEqual(sd(UUID_QINGPING, "0816a72514342d580104d800")["values"], {})
        self.assertEqual(sd(UUID_QINGPING, "0816a72514342d580104d800bb010201")["values"], {"temperature": 21.6, "humidity": 44.3})


class SwitchBotTest(unittest.TestCase):
    # Все векторы — https://github.com/sblibs/pySwitchbot/blob/master/tests/test_adv_parser.py

    # test_wosensor_passive_and_active: T/H берутся из manufacturer data, батарея — из service data
    def test_meter_active_and_passive(self):
        r = decode(None, {UUID_SWITCHBOT: H("5400e4069835")}, {0x0969: H("d7c17d5deb43de03069835")})
        self.assertEqual((r["brand"], r["model"], r["format"], r["encrypted"]), ("SwitchBot", "Meter", "switchbot", False))
        self.assertEqual(r["values"], {"temperature": 24.6, "humidity": 53, "battery": 100})

    # test_wosensor_active: только service data
    def test_meter_service_data_only(self):
        self.assertEqual(sd(UUID_SWITCHBOT, "5400e4069835")["values"], {"temperature": 24.6, "humidity": 53, "battery": 100})

    # Тот же пакет под старым UUID 0x0D00 (pySwitchbot перебирает fd3d, затем 0d00)
    def test_legacy_uuid(self):
        self.assertEqual(sd(UUID_SWITCHBOT_OLD, "5400e4069835")["model"], "Meter")

    # test_meter_pro_active
    def test_meter_pro(self):
        r = decode(None, {UUID_SWITCHBOT: H("340064")}, {0x0969: H("b0e9fe52dd84066408972c0005")})
        self.assertEqual(r["model"], "Meter Pro")
        self.assertEqual(r["values"], {"temperature": 23.8, "humidity": 44, "battery": 100})

    # test_meter_pro_c_active и test_meter_pro_c_co2_out_of_range_dropped
    def test_meter_pro_co2(self):
        r = decode(None, {UUID_SWITCHBOT: H("350064")}, {0x0969: H("b0e9fe543215b7e4079ba4003702d500")})
        self.assertEqual(r["model"], "Meter Pro CO2")
        self.assertEqual(r["values"], {"temperature": 27.7, "humidity": 36, "battery": 100, "co2": 725})
        r = decode(None, {UUID_SWITCHBOT: H("350064")}, {0x0969: H("b0e9fe543215b7e4079ba400379c4000")})
        self.assertNotIn("co2", r["values"])

    # test_wosensor_active_zero_data: нули — пустой пакет
    def test_zero_data(self):
        self.assertEqual(sd(UUID_SWITCHBOT, "540000000000")["values"], {})

    # test_wosensor_passive_only: без service data pySwitchbot узнаёт модель только из кэша — мы не узнаём
    def test_manufacturer_only_is_unknown(self):
        self.assertIsNone(decode(None, {}, {0x0969: H("d7c17d5deb43de03069835")}))

    # Синтетика по раскладке _sensor_th.py: бит 7 байта 1 сброшен → минус; 0x05 → .5
    def test_negative_temperature(self):
        self.assertEqual(sd(UUID_SWITCHBOT, "5400e405032a")["values"], {"temperature": -3.5, "humidity": 42, "battery": 100})

    def test_truncated(self):
        self.assertEqual(sd(UUID_SWITCHBOT, "54")["values"], {})
        self.assertEqual(sd(UUID_SWITCHBOT, "5400e406")["values"], {})
        # обрезанная manufacturer data — откат на service data
        r = decode(None, {UUID_SWITCHBOT: H("5400e4069835")}, {0x0969: H("d7c17d5deb43de0306")})
        self.assertEqual(r["values"], {"temperature": 24.6, "humidity": 53, "battery": 100})
        self.assertIsNone(sd(UUID_SWITCHBOT, "7600"))  # Hub 2 — не метр


class GoveeTest(unittest.TestCase):
    # Все векторы — https://github.com/Bluetooth-Devices/govee-ble/blob/main/tests/test_parser.py
    ROCKS = b"\x02\x15INTELLI_ROCKS_HWPu\xf2\xff\x0c"

    # GVH5075_SERVICE_INFO: к пакету приклеен iBeacon-хвост INTELLI_ROCKS
    def test_h5075(self):
        r = decode("GVH5075_2762", {}, {0xEC88: H("000341c264004c00") + self.ROCKS})
        self.assertEqual((r["brand"], r["model"], r["format"], r["encrypted"]), ("Govee", "H5075", "govee", False))
        self.assertEqual(r["values"], {"temperature": 21.3, "humidity": 44.2, "battery": 100})

    # GVH5072_SERVICE_INFO и GVH5072_75_GENERIC_SERVICE_INFO (модель только по company id)
    def test_h5072_and_generic(self):
        self.assertEqual(decode("GVH5072_ABCD", {}, {0xEC88: H("00034db26400")})["model"], "H5072")
        r = decode("Govee_ABCD", {}, {0xEC88: H("00034db26400")})
        self.assertEqual(r["model"], "H5072/H5075")
        self.assertEqual(r["values"], {"temperature": 21.6, "humidity": 49.8, "battery": 100})

    # GVH5075_SERVICE_INFO_NEGATIVE_VALUES: значение вне диапазона → только батарея
    def test_out_of_range(self):
        self.assertEqual(decode("GVH5075_2762", {}, {0xEC88: H("00bc00043e27")})["values"], {"battery": 62})

    # Синтетика по decode_temp_humid из govee-ble parser.py: бит 23 — минус; 53612 → −5.3 °C, 61.2 %
    def test_negative_temperature(self):
        r = decode("GVH5075_2762", {}, {0xEC88: H("0080d16c6400")})
        self.assertEqual(r["values"], {"temperature": -5.3, "humidity": 61.2, "battery": 100})

    # GVH5074_SERVICE_INFO: int16 LE ×0.01; рядом Apple-данные, их пропускаем
    def test_h5074(self):
        r = decode("Govee_H5074_5FF4", {}, {0xEC88: H("00e609bc126402"), 0x004C: self.ROCKS})
        self.assertEqual(r["model"], "H5074")
        self.assertEqual(r["values"], {"temperature": 25.34, "humidity": 47.96, "battery": 100})

    # GVH5100_SERVICE_INFO и GVH5177_SERVICE_INFO (company id 0x0001, данные с байта 2)
    def test_h5100_h5177(self):
        r = decode("GVH5100_7738", {}, {0x0001: H("010103465464")})
        self.assertEqual((r["model"], r["values"]), ("H5100", {"temperature": 21.4, "humidity": 61.2, "battery": 100}))
        r = decode("GVH5177_2EC8", {}, {0x0001: H("0101033626644c00") + b"\x02\x15INTELLI_ROCKS_HWQw\xf2\xff\xc2"})
        self.assertEqual((r["model"], r["values"]), ("H5177", {"temperature": 21.0, "humidity": 47.0, "battery": 100}))

    # GVH5108_SERVICE_INFO_NO_NAME: 8 байт под 0x0001 — H5108 даже без имени
    def test_h5108_no_name(self):
        r = decode("G", {}, {0x0001: H("010103c730640000"), 0x004C: self.ROCKS})
        self.assertEqual((r["model"], r["values"]), ("H5108", {"temperature": 24.7, "humidity": 60.0, "battery": 100}))

    # GVH5051_SERVICE_INFO: 9 байт под 0xEC88, пустое имя
    def test_h5051(self):
        r = decode("", {}, {0xEC88: H("00ba0af90f63020101")})
        self.assertEqual((r["model"], r["values"]), ("H5051", {"temperature": 27.46, "humidity": 40.89, "battery": 99}))

    def test_truncated(self):
        self.assertIsNone(decode("GVH5075_2762", {}, {0xEC88: H("000341c264")}))
        self.assertIsNone(decode("GVH5075_2762", {}, {0xEC88: b""}))
        self.assertIsNone(decode("GVH5100_7738", {}, {0x0001: H("0101034654")}))
        self.assertIsNone(decode(None, {}, {0x0001: H("010103465464")}))  # 6 байт без имени — не узнать


class GeneralTest(unittest.TestCase):
    # Apple iBeacon (company 0x004C) — не датчик
    def test_apple_is_none(self):
        self.assertIsNone(decode("iPhone", {}, {0x004C: H("0215") + bytes(21)}))
        self.assertIsNone(decode(None, {}, {}))
        self.assertIsNone(decode(None, None, None))

    def test_unrelated_service_data(self):
        self.assertIsNone(decode("x", {"0000180f-0000-1000-8000-00805f9b34fb": H("64")}, {}))

    def test_uppercase_uuid_and_bytearray(self):
        r = decode(None, {UUID_BTHOME.upper(): bytearray(H("4002c40903bf13"))}, {})
        self.assertEqual(r["values"]["temperature"], 25.0)

    def test_garbage_never_raises(self):
        vectors = [
            (UUID_BTHOME, "4000090102ca0903bf13530c48656c6c6f20576f726c64213b0103053a04"),
            (UUID_ATC, "f4830238c1a4a9066911b60b58f70d"),
            (UUID_ATC, "a4c1380283f400a22f5f0bf819"),
            (UUID_MIBEACON, "7120980012f34f6b8d7cc40d041002c400"),
            (UUID_MIBEACON, "5020aa01a3bf2e3b342d580d1004b40095020a10013b"),
            (UUID_QINGPING, "0a5d931d86342d5801041701ce010201641302b302"),
            (UUID_SWITCHBOT, "5400e4069835"),
        ]
        rnd = random.Random(42)
        names = [None, "", "GVH5075_2762", "GVH5100_1", "Govee_H5074", "SBHT-003C", "ATC_123456", "G"]
        for uuid, hexdata in vectors:
            data = H(hexdata)
            for cut in range(len(data) + 1):
                decode(None, {uuid: data[:cut]}, {0x0969: data[:cut]})
            for _ in range(300):
                junk = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 32)))
                decode(rnd.choice(names), {uuid: junk}, {rnd.choice([0xEC88, 0x0001, 0x0969]): junk})
        for _ in range(500):
            junk = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 40)))
            decode(rnd.choice(names), {}, {rnd.choice([0xEC88, 0x0001, 0x0969, 0x004C]): junk})
        # Неверные типы на входе — тоже без исключений
        self.assertIsNone(decode(123, {"x": None}, {"bad": b""}))


if __name__ == "__main__":
    unittest.main()
