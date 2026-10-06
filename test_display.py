import unittest
import threading
from unittest.mock import MagicMock, patch

from PIL import Image

from button_bridge import handle_event, open_event_app, default_calendar_app, TODAY_URL, AutoRefresh
from anzeigen import send_frames
from display_render import BIT_REVERSE, PAGE_BYTES, WIDTH, font, markdown_rows, pack_page, render_pages


class DisplayTests(unittest.TestCase):
    def test_markdown_styles_links_and_checkboxes(self):
        rows = markdown_rows("[ ] **Fett** *Kursiv* ~~Alt~~ `Code` [Link](https://example.com) ==Markiert==\n\n____\n\n- [x] Fertig")
        self.assertEqual(rows[0].marker, "checkbox")
        spans = [span for row in rows for span in row.spans]
        by_text = {span.text.strip(): span.style for span in spans if span.text.strip()}
        self.assertTrue(by_text["Fett"].bold)
        self.assertTrue(by_text["Kursiv"].italic)
        self.assertTrue(by_text["Alt"].strike)
        self.assertTrue(by_text["Code"].code)
        self.assertTrue(by_text["Link"].underline)
        self.assertTrue(by_text["Markiert"].bold)
        self.assertTrue(any(row.rule for row in rows))
        self.assertEqual(rows[-1].marker, "checked")
        self.assertFalse(any("https://example.com" in span.text for span in spans))

    def test_long_styled_unicode_words_wrap_without_losing_characters(self):
        word = "Äöüß" * 60
        rows = markdown_rows(f"[ ] **{word}**")
        self.assertGreater(len(rows), 1)
        self.assertEqual("".join(s.text for row in rows for s in row.spans), word)
        for row in rows:
            self.assertLessEqual(sum(font(s.style).getlength(s.text) for s in row.spans), WIDTH - 20 - row.indent)

    def test_bitmap_roundtrip_keeps_polarity_and_bit_order(self):
        image = Image.new("1", (200, 300), 1)
        image.putpixel((0, 0), 0)
        image.putpixel((7, 0), 0)
        image.putpixel((8, 1), 0)
        data = pack_page(image)
        self.assertEqual(len(data), PAGE_BYTES)
        self.assertEqual(data[0], 0x81)
        self.assertEqual(data[26], 0x01)
        reconstructed = Image.frombytes("1", (200, 300), data.translate(BIT_REVERSE))
        self.assertTrue(reconstructed.getpixel((0, 0)))  # 1 means black ink on the board.
        self.assertFalse(reconstructed.getpixel((1, 0)))

    def test_pagination_repeats_header_and_draws_real_checkbox(self):
        pages = render_pages("<!-- agenda-stamp: 07.10.2026 12:00 -->\n\n" + "\n\n".join(f"[ ] Aufgabe {n}" for n in range(40)))
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertEqual(page.crop((0, 0, 200, 30)).tobytes(), pages[0].crop((0, 0, 200, 30)).tobytes())
            self.assertEqual(page.getpixel((0, 0)), 255)
        self.assertEqual(pages[0].getpixel((10, 38)), 0)
        self.assertEqual(pages[0].getpixel((14, 42)), 255)

    def test_only_today_button_event_opens_fixed_ticktick_url(self):
        with patch("button_bridge.APP_OPENER.submit") as submit:
            self.assertFalse(handle_event(b"BUTTON TODAY ticktick://malicious"))
            self.assertFalse(handle_event(b"BUTTON CALENDAR /tmp/arbitrary.app"))
            submit.assert_not_called()
            self.assertTrue(handle_event(b"BUTTON CALENDAR"))
            self.assertTrue(handle_event(b"BUTTON TODAY"))
            self.assertEqual(submit.call_count, 2)
        with patch("button_bridge.subprocess.run") as run:
            run.return_value.returncode = 0
            open_event_app(b"BUTTON TODAY")
            run.assert_called_once_with(["/usr/bin/open", TODAY_URL], capture_output=True, timeout=10)

    def test_calendar_event_opens_default_app_without_importing_a_file(self):
        with patch("button_bridge.default_calendar_app", return_value="/Applications/Custom Calendar.app"), \
             patch("button_bridge.subprocess.run") as run:
            run.return_value.returncode = 0
            open_event_app(b"BUTTON CALENDAR")
            run.assert_called_once_with(["/usr/bin/open", "-a", "/Applications/Custom Calendar.app"],
                                        capture_output=True, timeout=10)

    def test_calendar_import_helper_falls_back_to_apple_calendar(self):
        with patch("button_bridge.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "/System/Library/CoreServices/CalendarFileHandler.app\n"
            self.assertEqual(default_calendar_app(), "/System/Applications/Calendar.app")
            run.return_value.stdout = "/Applications/Custom Calendar.app\n"
            with patch("button_bridge.Path.is_dir", return_value=True):
                self.assertEqual(default_calendar_app(), "/Applications/Custom Calendar.app")

    def test_queued_button_survives_display_upload(self):
        device = MagicMock()
        device.read_until.side_effect = [b"BUTTON CALENDAR\n", b"AGENDA3\n", b"READY\n",
                                        b"READY\n", b"STORED 1 0\n", b"OK 1 1\n"]
        device.write.side_effect = len
        with patch("anzeigen.port_lock"), patch("anzeigen.serial.Serial") as serial_port, \
             patch("button_bridge.APP_OPENER.submit") as submit:
            serial_port.return_value.__enter__.return_value = device
            self.assertEqual(send_frames([bytes(PAGE_BYTES)], "test"), b"OK 1 1")
            submit.assert_called_once_with(open_event_app, b"BUTTON CALENDAR")
            device.reset_input_buffer.assert_not_called()

    def test_upload_recovers_from_partial_command_after_board_reset(self):
        device = MagicMock()
        device.read_until.side_effect = [b"ERROR command\n", b"BUTTON TODAY\n", b"AGENDA3\n",
                                        b"READY\n", b"READY\n", b"STORED 1 0\n", b"OK 1 1\n"]
        device.write.side_effect = len
        with patch("anzeigen.port_lock"), patch("anzeigen.serial.Serial") as serial_port, \
             patch("button_bridge.APP_OPENER.submit") as submit:
            serial_port.return_value.__enter__.return_value = device
            self.assertEqual(send_frames([bytes(PAGE_BYTES)], "test"), b"OK 1 1")
            self.assertEqual(device.write.call_args_list[0].args[0], b"\nHELLO\n")
            submit.assert_called_once_with(open_event_app, b"BUTTON TODAY")

    def test_protocol_sync_does_not_ignore_a_later_checksum_error(self):
        device = MagicMock()
        device.read_until.side_effect = [b"ERROR command\n", b"AGENDA3\n", b"READY\n",
                                        b"READY\n", b"ERROR checksum\n"]
        device.write.side_effect = len
        with patch("anzeigen.port_lock"), patch("anzeigen.serial.Serial") as serial_port:
            serial_port.return_value.__enter__.return_value = device
            with self.assertRaisesRegex(RuntimeError, "ERROR checksum"):
                send_frames([bytes(PAGE_BYTES)], "test")
            self.assertFalse(any(call.args[0] == b"COMMIT\n" for call in device.write.call_args_list))


class RefreshTests(unittest.TestCase):
    def test_explicit_refresh_works_when_periodic_timer_is_disabled(self):
        calls = []
        refresh = AutoRefresh(lambda: calls.append(1), interval=0)
        refresh.poll()
        self.assertEqual(calls, [])
        refresh.request_now()
        refresh.poll(connected=False)
        self.assertEqual(calls, [])
        refresh.poll()
        refresh.worker.join(1)
        refresh.poll()
        self.assertEqual(calls, [1])

    def test_refresh_every_minute_without_overlapping_slow_requests(self):
        now = [100.0]
        began, release = threading.Event(), threading.Event()
        calls = []
        def action():
            calls.append(now[0])
            began.set()
            release.wait(2)
        refresh = AutoRefresh(action, clock=lambda: now[0])
        now[0] = 159
        refresh.poll()
        self.assertEqual(calls, [])
        now[0] = 160
        refresh.poll()
        self.assertTrue(began.wait(1))
        now[0] = 220
        refresh.poll()
        self.assertEqual(calls, [160])
        release.set()
        refresh.worker.join(1)
        refresh.poll()
        refresh.worker.join(1)
        self.assertEqual(calls, [160, 220])

    def test_offline_does_not_fetch_and_reconnection_refreshes_immediately(self):
        now = [0.0]
        calls = []
        refresh = AutoRefresh(lambda: calls.append(now[0]), clock=lambda: now[0])
        now[0] = 100
        refresh.poll(connected=False)
        self.assertEqual(calls, [])
        refresh.request_now()
        refresh.poll()
        refresh.worker.join(1)
        self.assertEqual(calls, [100])
        refresh.set_interval(0)
        now[0] = 1000
        refresh.poll()
        self.assertEqual(calls, [100])


if __name__ == "__main__":
    unittest.main()
