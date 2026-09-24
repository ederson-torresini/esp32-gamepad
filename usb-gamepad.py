"""
USB HID Gamepad for a native-USB ESP32 (ESP32-S2 / ESP32-S3) running MicroPython.

Controls : D-pad (hat switch) + A, B, X, Y, L, R, Select, Start
Modes    :
  GAMING  - (default) the board enumerates as a pure USB gamepad.
  SERVICE - hold START while pressing reset / plugging in. The board does NOT
            start the gamepad; the normal USB serial REPL stays available so
            you can update the code with mpremote / Thonny.

Requires: MicroPython with USB device support (machine.USBDevice) and the
          usb-device-hid package:   mpremote mip install usb-device-hid

Wiring: every button between its GPIO and GND (internal pull-ups are used).
"""

from machine import Pin
from time import sleep_ms

# ----------------------------------------------------------------- config ---
# D-pad
PIN_UP, PIN_DOWN, PIN_LEFT, PIN_RIGHT = 4, 5, 6, 7

# Order defines the HID button number (bit 0 = button 1, ...)
BUTTON_ORDER = ("A", "B", "X", "Y", "L", "R", "SELECT", "START")
BUTTON_PINS = {
    "A": 8,
    "B": 9,
    "X": 10,
    "Y": 11,
    "L": 12,
    "R": 13,
    "SELECT": 14,
    "START": 15,
}
# Avoid on ESP32-S3: GPIO19/20 (USB), 0/3/45/46 (strapping), 26-37 (flash/PSRAM).

LED_PIN = 2  # set to None if you don't have an LED here

# -------------------------------------------------------------- hardware ---
led = Pin(LED_PIN, Pin.OUT) if LED_PIN is not None else None
dpad = [Pin(p, Pin.IN, Pin.PULL_UP) for p in (PIN_UP, PIN_RIGHT, PIN_DOWN, PIN_LEFT)]
buttons = [Pin(BUTTON_PINS[n], Pin.IN, Pin.PULL_UP) for n in BUTTON_ORDER]
BIT_START = 1 << BUTTON_ORDER.index("START")

# Hat values: 0 = centered, 1 = N, 2 = NE, 3 = E, 4 = SE, 5 = S, 6 = SW, 7 = W, 8 = NW
# index bits: up=1, right=2, down=4, left=8
HAT_TABLE = {1: 1, 3: 2, 2: 3, 6: 4, 4: 5, 12: 6, 8: 7, 9: 8}


def read_inputs():
    """Return (button_bits, hat_value); opposite d-pad directions cancel out."""
    bits = 0
    for i, pin in enumerate(buttons):
        if not pin.value():
            bits |= 1 << i
    up, right, down, left = [not p.value() for p in dpad]
    if up and down:
        up = down = False
    if left and right:
        left = right = False
    idx = up | (right << 1) | (down << 2) | (left << 3)
    return bits, HAT_TABLE.get(idx, 0)


def set_led(v):
    if led is not None:
        led.value(v)


# ----------------------------------------------------------- SERVICE mode ---
def service_mode():
    """Leave USB alone (built-in serial REPL) and blink so you know where you are."""
    print("SERVICE mode: gamepad disabled, REPL available.")
    while True:
        set_led(1)
        sleep_ms(80)
        set_led(0)
        sleep_ms(920)


# ------------------------------------------------------------ GAMING mode ---
def gaming_mode():
    import usb.device
    from usb.device.hid import HIDInterface

    # 8 buttons + 1 hat switch, 2-byte report, no report ID
    report_map = bytes((
        0x05, 0x01,        # Usage Page (Generic Desktop)
        0x09, 0x05,        # Usage (Game Pad)
        0xA1, 0x01,        # Collection (Application)
        0x05, 0x09,        #   Usage Page (Button)
        0x19, 0x01,        #   Usage Minimum (1)
        0x29, 0x08,        #   Usage Maximum (8)
        0x15, 0x00,        #   Logical Minimum (0)
        0x25, 0x01,        #   Logical Maximum (1)
        0x75, 0x01,        #   Report Size (1)
        0x95, 0x08,        #   Report Count (8)
        0x81, 0x02,        #   Input (Data, Var, Abs)
        0x05, 0x01,        #   Usage Page (Generic Desktop)
        0x09, 0x39,        #   Usage (Hat switch)
        0x15, 0x01,        #   Logical Minimum (1)
        0x25, 0x08,        #   Logical Maximum (8)
        0x35, 0x00,        #   Physical Minimum (0)
        0x46, 0x3B, 0x01,  #   Physical Maximum (315)
        0x65, 0x14,        #   Unit (Degrees)
        0x75, 0x04,        #   Report Size (4)
        0x95, 0x01,        #   Report Count (1)
        0x81, 0x42,        #   Input (Data, Var, Abs, Null state)
        0x75, 0x04,        #   Report Size (4)
        0x95, 0x01,        #   Report Count (1)
        0x81, 0x03,        #   Input (Const) - padding
        0xC0,              # End Collection
    ))

    class Gamepad(HIDInterface):
        def __init__(self):
            super().__init__(report_map, interface_str="MicroPython Gamepad")

        def send(self, bits, hat):
            return self.send_report(bytes((bits, hat)), timeout_ms=10)

    pad = Gamepad()
    # builtin_driver=False -> the device is a pure gamepad (no serial port)
    usb.device.get().init(pad, builtin_driver=False)

    state = (0, 0)
    candidate = state
    stable = 0
    dirty = True       # send the current state as soon as the host is ready
    was_open = False

    while True:
        # Debounce: state must be stable for 3 samples (~15 ms)
        raw = read_inputs()
        if raw == candidate:
            stable += 1
        else:
            candidate, stable = raw, 0
        if stable >= 3 and candidate != state:
            state = candidate
            dirty = True

        is_open = pad.is_open()
        if is_open and not was_open:
            dirty = True  # host just configured us: resync
        was_open = is_open

        if is_open and dirty:
            dirty = not pad.send(*state)  # retry next loop if the send failed

        set_led(1 if is_open else 0)
        sleep_ms(5)


# ------------------------------------------------------------------ boot ---
# Hold START during boot to enter SERVICE mode
sleep_ms(50)  # let the pull-ups settle
if read_inputs()[0] & BIT_START:
    service_mode()
else:
    gaming_mode()