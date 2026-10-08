import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker  # noqa: E402
import control  # noqa: E402
import strategies  # noqa: E402

# Устроен как general*.bat у flowseal: start … winws.exe, строки склеены через ^, пути в кавычках.
BAT = r'''@echo off
chcp 65001 > nul
set "BIN=%~dp0bin\"
set "LISTS=%~dp0lists\"
start "zapret: %~n0" /min "%BIN%winws.exe" --wf-tcp=80,443,%GameFilterTCP% --wf-udp=443,50000-50100,%GameFilterUDP% ^
--filter-udp=443 --hostlist="%LISTS%list-general.txt" --hostlist="%LISTS%list-general-user.txt" --dpi-desync=fake --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin" --new ^
--filter-tcp=443 --ipset="%LISTS%ipset-all.txt" --dpi-desync=multisplit --dpi-desync-fake-tls=^! --dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com --new ^
--filter-tcp=%GameFilterTCP% --dpi-desync=fake --dpi-desync-any-protocol=1
'''
R, L = Path("/rel"), Path("/loc")


class Convert(unittest.TestCase):
    def test_profiles_and_ports(self):
        s = strategies.convert(BAT, R, L)
        self.assertEqual(s["tcp"], "80,443")  # порт-заглушка 12 nftables не нужен
        self.assertEqual(s["udp"], "443,50000-50100")
        self.assertEqual(len(s["profiles"]), 3)
        self.assertEqual(s["profiles"][0][:3], ["--filter-udp=443", "--hostlist=/rel/lists/list-general.txt",
                                                "--hostlist=/loc/list-general-user.txt"])
        self.assertIn("--dpi-desync-fake-quic=/rel/bin/quic_initial_www_google_com.bin", s["profiles"][0])
        self.assertIn("--ipset=/loc/ipset-all.txt", s["profiles"][1])  # режим IPSet — свой файл
        self.assertEqual(s["profiles"][2][0], "--filter-tcp=12")  # игровой фильтр выключен — как у flowseal

    def test_compat_and_note(self):
        s = strategies.convert(BAT, R, L)
        self.assertIn("--dpi-desync-fake-tls=!", s["profiles"][1])
        self.assertEqual(len(s["notes"]), 1)

    def test_game_filter(self):
        s = strategies.convert(BAT, R, L, game="all")
        self.assertEqual(s["tcp"], "80,443,1024-65535")
        self.assertEqual(s["udp"], "443,50000-50100,1024-65535")
        self.assertEqual(s["profiles"][2][0], "--filter-tcp=1024-65535")
        self.assertEqual(strategies.convert(BAT, R, L, game="udp")["tcp"], "80,443")
        with self.assertRaises(strategies.StrategyError):
            strategies.convert(BAT, R, L, game="maybe")

    def test_dangerous_arguments_rejected(self):
        for evil in ['--dpi-desync=fake";reboot;"', "--hostlist=$(id)", "--x=`id`", "--x=a b"]:
            bat = BAT.replace("--dpi-desync-any-protocol=1", evil)
            with self.assertRaises(strategies.StrategyError, msg=evil):
                strategies.convert(bat, R, L)

    def test_unknown_variable_rejected(self):
        with self.assertRaises(strategies.StrategyError):
            strategies.convert(BAT.replace("--dpi-desync-any-protocol=1", "--x=%SECRET%"), R, L)

    def test_needs_exactly_one_winws(self):
        with self.assertRaises(strategies.StrategyError):
            strategies.convert("@echo off\n", R, L)

    def test_nfqws_opt_and_order(self):
        self.assertEqual(strategies.nfqws_opt([["--a"], ["--b", "--c"]]), "\n--a --new\n--b --c\n")
        names = ["general (SIMPLE FAKE)", "general (ALT10)", "general", "general (ALT2)", "general (ALT)", "general (EXP)"]
        self.assertEqual(sorted(names, key=strategies.sort_key),
                         ["general", "general (ALT)", "general (ALT2)", "general (ALT10)", "general (EXP)",
                          "general (SIMPLE FAKE)"])

    def test_load_folder(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "general.bat").write_text(BAT)
            (Path(d) / "general (BAD).bat").write_text("nothing here")
            (Path(d) / "service.bat").write_text(BAT)  # не стратегия
            found = strategies.load(d, L)
        self.assertEqual([s["name"] for s in found], ["general", "general (BAD)"])
        self.assertIn("error", found[1])


class Lists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(control, "LOCAL", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_domains_saved_and_placeholder_hidden(self):
        control.write_list("list-general-user.txt", "Example.com\nrutracker.org, *.foo.net\nexample.com")
        self.assertEqual(control.read_list("list-general-user.txt"), "example.com\nrutracker.org\n*.foo.net")
        control.write_list("list-general-user.txt", "")
        body = (Path(self.tmp.name) / "list-general-user.txt").read_text()
        self.assertIn("domain.example.abc", body)  # пустой список nfqws не примет — заглушка flowseal
        self.assertEqual(control.read_list("list-general-user.txt"), "")

    def test_bad_lines_explained(self):
        for name, line, text in [("list-general-user.txt", "https://youtube.com", "без http"),
                                 ("list-general-user.txt", "bad domain", "не домен"),
                                 ("ipset-exclude-user.txt", "example.com", "не адрес"),
                                 ("nope.txt", "a.b", "нет такого")]:
            with self.assertRaises(ValueError) as e:
                control.write_list(name, line)
            self.assertIn(text, str(e.exception))
        control.write_list("ipset-exclude-user.txt", "203.0.113.0/24\n198.51.100.7")

    def test_ipset_modes(self):
        control.ensure_local("none")
        self.assertEqual((Path(self.tmp.name) / "ipset-all.txt").read_text().strip(), control.IPSET_NONE)
        control.ensure_local("any")
        self.assertEqual((Path(self.tmp.name) / "ipset-all.txt").read_text(), "")
        for name in strategies.USER_LISTS:
            self.assertTrue((Path(self.tmp.name) / name).exists())


class Settings(unittest.TestCase):
    def test_validate(self):
        with mock.patch.object(control, "load_settings", return_value=dict(control.DEFAULTS)):
            s = control.validate({"strategy": "general (ALT9)", "game": "tcp", "evil": 1}, ["general", "general (ALT9)"])
            self.assertEqual((s["strategy"], s["game"]), ("general (ALT9)", "tcp"))
            self.assertNotIn("evil", s)
            for bad in [{"strategy": "rm -rf"}, {"game": "x"}, {"ipset": "all"}]:
                with self.assertRaises(ValueError):
                    control.validate(bad, ["general"])

    def test_config(self):
        s = strategies.convert(BAT, R, L)
        conf = control.build_config({"name": "general", **s})
        self.assertIn("NFQWS_PORTS_TCP=80,443\n", conf)
        self.assertIn("FWTYPE=nftables\n", conf)
        self.assertIn('NFQWS_OPT="\n--filter-udp=443 ', conf)


class Check(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(checker.classify("200", 0, 65536, ""), "ok")
        self.assertEqual(checker.classify("200", 18, 900, ""), "ok")
        self.assertEqual(checker.classify("000", 28, 0, ""), "blocked")
        self.assertEqual(checker.classify("200", 28, 16 * 1024, ""), "cut")  # встало на 16 КБ — ТСПУ
        self.assertEqual(checker.classify("200", 28, 300 * 1024, ""), "ok")  # просто большая страница
        self.assertEqual(checker.classify("000", 35, 0, "SSL connection reset"), "blocked")
        self.assertEqual(checker.classify("000", 60, 0, "SSL certificate problem: self-signed"), "ssl")

    def test_score(self):
        rows = [{"name": "A", "TLS1.2": {"status": "ok"}, "TLS1.3": {"status": "blocked"}},
                {"name": "P", "ping": {"status": "ok"}}]
        self.assertEqual((checker.score(rows), checker.total(rows)), (1, 2))

    def test_targets_format(self):
        text = '# c\nYouTubeWeb = "https://www.youtube.com"\nDNS1 = "PING:1.1.1.1"\nbad line\n'
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "utils").mkdir()
            (Path(d) / "utils/targets.txt").write_text(text)
            with mock.patch.object(control, "RELEASE", Path(d)):
                self.assertEqual(checker.targets(), [("YouTubeWeb", "https://www.youtube.com"), ("DNS1", "PING:1.1.1.1")])


if __name__ == "__main__":
    unittest.main()
