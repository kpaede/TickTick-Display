#include "ST7305_U8g2.h"

static ST7305_U8g2 lcd(11, 12, 5, 40, 41);
static U8G2 *screen;
constexpr unsigned PAGE_BYTES = 200 * 300 / 8, MAX_PAGES = 64;
// Physical left/right when viewed in our landscape display orientation.
constexpr int LEFT_BUTTON_PIN = 18, RIGHT_BUTTON_PIN = 0;
struct Pane {
  uint8_t *pages = nullptr, *pending = nullptr;
  unsigned count = 0, pendingCount = 0, page = 0;
  bool ready[MAX_PAGES];
};
static Pane panes[2]; // 0: calendar on the left, 1: TickTick on the right.
static uint8_t incoming[PAGE_BYTES];
static unsigned incomingPane = 0, incomingPage = 0, receivedBytes = 0;
static bool receiving = false, transaction = false;
static uint32_t expectedCrc = 0, lastByteMs = 0;
static String command;

static uint32_t crc32(const uint8_t *data, size_t size) {
  uint32_t crc = 0xffffffff;
  for (size_t i = 0; i < size; ++i) {
    crc ^= data[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return crc ^ 0xffffffff;
}

static void drawPage() {
  screen->clearBuffer();
  // Manufacturer's inverted ST7305 mode: set pixels are white.
  screen->setDrawColor(1);
  screen->drawBox(0, 0, 400, 300);
  screen->setDrawColor(0);
  screen->setBitmapMode(1);
  screen->setFontMode(1);
  for (unsigned index = 0; index < 2; ++index) {
    Pane &pane = panes[index];
    unsigned x = index * 200;
    if (pane.pages) {
      screen->drawXBMP(x, 0, 200, 300, pane.pages + pane.page * PAGE_BYTES);
    } else {
      screen->setFont(u8g2_font_helvB14_tf);
      screen->drawUTF8(x + 10, 23, index == 0 ? "Kalender" : "Tasks");
      screen->setFont(u8g2_font_helvR10_tf);
      screen->drawUTF8(x + 10, 53, "USB bereit.");
      screen->drawUTF8(x + 10, 85, "Warte auf den Mac.");
    }
  }
  screen->sendBuffer();
}

static void nextPage(unsigned index) {
  Pane &pane = panes[index];
  if (pane.count > 1) { pane.page = (pane.page + 1) % pane.count; drawPage(); }
}

static void clearPending() {
  for (Pane &pane : panes) {
    free(pane.pending);
    pane.pending = nullptr;
    pane.pendingCount = 0;
  }
  receiving = transaction = false;
}

static void handleCommand() {
  unsigned left, right, index, number;
  unsigned long checksum;
  char extra;
  if (command == "HELLO") {
    Serial.println("AGENDA3");
  } else if (command == "STATUS") {
    Serial.printf("STATE %u/%u %u/%u\n", panes[0].page + 1, panes[0].count,
                  panes[1].page + 1, panes[1].count);
  } else if (command.startsWith("BEGIN ")
             && sscanf(command.c_str(), "BEGIN %u %u %c", &left, &right, &extra) == 2
             && left <= MAX_PAGES && right <= MAX_PAGES && (left || right)) {
    clearPending();
    unsigned counts[] = {left, right}; // 0 leaves that pane unchanged.
    bool allocated = true;
    for (unsigned i = 0; i < 2; ++i) {
      Pane &pane = panes[i];
      pane.pendingCount = counts[i];
      memset(pane.ready, 0, sizeof(pane.ready));
      if (counts[i]) {
        pane.pending = (uint8_t *)ps_malloc(counts[i] * PAGE_BYTES);
        allocated &= pane.pending != nullptr;
      }
    }
    if (!allocated) { clearPending(); Serial.println("ERROR memory"); }
    else { transaction = true; Serial.println("READY"); }
  } else if (command.startsWith("PAGE ")
             && sscanf(command.c_str(), "PAGE %u %u %lx %c", &index, &number, &checksum, &extra) == 3
             && index < 2 && transaction && panes[index].pending && number < panes[index].pendingCount) {
    incomingPane = index;
    incomingPage = number;
    expectedCrc = checksum;
    receivedBytes = 0;
    receiving = true;
    lastByteMs = millis();
    panes[index].ready[number] = false;
    Serial.println("READY");
  } else if (command == "COMMIT" && transaction) {
    bool complete = true;
    for (Pane &pane : panes)
      for (unsigned i = 0; i < pane.pendingCount; ++i) complete &= pane.ready[i];
    if (!complete) { Serial.println("ERROR incomplete"); }
    else {
      for (Pane &pane : panes) {
        if (!pane.pendingCount) continue;
        free(pane.pages);
        pane.pages = pane.pending;
        pane.pending = nullptr;
        pane.count = pane.pendingCount;
        pane.pendingCount = 0;
        if (pane.page >= pane.count) pane.page = 0;
      }
      transaction = false;
      drawPage();
      Serial.printf("OK %u %u\n", panes[0].count, panes[1].count);
    }
  } else { Serial.println("ERROR command"); }
  command = "";
}

struct Button {
  int pin;
  bool raw = false, stable = false, waitingClick = false, secondClick = false;
  uint32_t changedMs = 0, releasedMs = 0;
  void poll(unsigned paneIndex, const char *doubleEvent) {
    uint32_t now = millis();
    bool pressed = digitalRead(pin) == LOW;
    if (pressed != raw) { raw = pressed; changedMs = now; }
    if (stable != raw && now - changedMs >= 20) {
      stable = raw;
      if (stable) {
        if (waitingClick) {
          if (changedMs - releasedMs <= 320) { secondClick = true; }
          else { waitingClick = false; nextPage(paneIndex); }
        }
      } else {
        if (secondClick) {
          secondClick = false;
          waitingClick = false;
          Serial.println(doubleEvent);
        } else { waitingClick = true; releasedMs = now; }
      }
    }
    if (waitingClick && !stable && !raw && now - releasedMs > 320) {
      waitingClick = false;
      nextPage(paneIndex);
    }
  }
};
static Button leftButton{LEFT_BUTTON_PIN}, rightButton{RIGHT_BUTTON_PIN};

void setup() {
  Serial.setRxBufferSize(PAGE_BYTES + 256);
  Serial.begin(115200);
  pinMode(LEFT_BUTTON_PIN, INPUT_PULLUP);
  pinMode(RIGHT_BUTTON_PIN, INPUT_PULLUP);
  delay(300);
  lcd.begin(0, U8G2_R1);
  screen = lcd.getU8g2();
  drawPage();
}

void loop() {
  while (Serial.available()) {
    uint8_t c = Serial.read();
    if (receiving) {
      incoming[receivedBytes++] = c;
      lastByteMs = millis();
      if (receivedBytes == PAGE_BYTES) {
        receiving = false;
        if (crc32(incoming, PAGE_BYTES) != expectedCrc) { Serial.println("ERROR checksum"); }
        else {
          Pane &pane = panes[incomingPane];
          memcpy(pane.pending + incomingPage * PAGE_BYTES, incoming, PAGE_BYTES);
          pane.ready[incomingPage] = true;
          Serial.printf("STORED %u %u\n", incomingPane, incomingPage);
        }
      }
    } else if (c == '\n') { handleCommand(); }
    else if (c != '\r') {
      command += (char)c;
      if (command.length() > 80) { command = ""; Serial.println("ERROR command"); }
    }
  }
  if (receiving && millis() - lastByteMs > 10000) { clearPending(); Serial.println("ERROR timeout"); }
  leftButton.poll(0, "BUTTON CALENDAR");
  rightButton.poll(1, "BUTTON TODAY");
  delay(1);
}
