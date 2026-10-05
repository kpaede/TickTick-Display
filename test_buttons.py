"""Execute the actual firmware button code with a simulated clock and GPIO."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang++")
        if not compiler:
            raise unittest.SkipTest("clang++ wird für die Firmware-Tastentests benötigt.")
        firmware = (Path(__file__).parent / "firmware" / "firmware.ino").read_text()
        button = firmware[firmware.index("struct Button {"):firmware.index("static Button leftButton")]
        cls.temporary = tempfile.TemporaryDirectory(prefix="ticktick-button-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        source = root / "buttons.cpp"
        cls.executable = root / "buttons"
        source.write_text('''#include <stdint.h>
#include <stdio.h>
constexpr int LOW = 0;
uint32_t currentTime = 0;
bool pressed = false;
uint32_t millis() { return currentTime; }
int digitalRead(int) { return pressed ? LOW : 1; }
void nextPage(unsigned pane) { printf("PAGE %u\\n", pane); }
struct Output { void println(const char *text) { puts(text); } } Serial;
''' + button + '''
int main(int argc, char **argv) {
    unsigned pane = argv[1][0] == '0' ? 0 : 1;
    Button button{18};
    unsigned state;
    while (scanf("%u %u", &currentTime, &state) == 2) {
        pressed = state != 0;
        button.poll(pane, pane == 0 ? "BUTTON CALENDAR" : "BUTTON TODAY");
    }
}
''')
        result = subprocess.run([compiler, "-std=c++17", str(source), "-o", str(cls.executable)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)

    def actions(self, pane, steps):
        result = subprocess.run([str(self.executable), str(pane)],
                                input="".join(f"{now} {int(pressed)}\n" for now, pressed in steps),
                                capture_output=True, text=True, check=True)
        return result.stdout.splitlines()

    def test_single_click_pages_only_its_own_pane(self):
        steps = [(0, True), (20, True), (40, False), (60, False), (381, False)]
        for pane in (0, 1):
            self.assertEqual(self.actions(pane, steps), [f"PAGE {pane}"])

    def test_double_click_opens_app_without_advancing_either_pane(self):
        steps = [(0, True), (20, True), (40, False), (60, False),
                 (150, True), (170, True), (190, False), (210, False), (600, False)]
        self.assertEqual(self.actions(0, steps), ["BUTTON CALENDAR"])
        self.assertEqual(self.actions(1, steps), ["BUTTON TODAY"])

    def test_second_press_near_deadline_can_be_held_without_losing_double_click(self):
        steps = [(0, True), (20, True), (40, False), (60, False),
                 (375, True), (381, True), (395, True), (900, False), (920, False), (1500, False)]
        self.assertEqual(self.actions(0, steps), ["BUTTON CALENDAR"])

    def test_bounce_is_ignored_and_two_separate_clicks_page_twice(self):
        self.assertEqual(self.actions(0, [(0, True), (5, False), (25, False), (500, False)]), [])
        steps = [(0, True), (20, True), (40, False), (60, False), (381, False),
                 (500, True), (520, True), (540, False), (560, False), (881, False)]
        self.assertEqual(self.actions(0, steps), ["PAGE 0", "PAGE 0"])


if __name__ == "__main__":
    unittest.main()
