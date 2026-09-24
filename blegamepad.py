"""
BLE HID Gamepad for NodeMCU ESP32 (MicroPython)

Controls : D-pad (hat switch) + A, B, X, Y, L, R, Select, Start
Modes    :
  PAIRING - advertises to anyone, wipes the old bond, and accepts ONE new device.
            Once that device bonds, the gamepad switches to GAMING mode.
  GAMING  - only the bonded device is accepted. Any other device is refused
            (its pairing keys are rejected and the link is dropped).

Enter PAIRING mode at any time by holding START + SELECT for 3 seconds.
If no bond is stored (first boot), the gamepad starts in PAIRING mode.

LED (GPIO2): fast blink = pairing, slow blink = gaming/waiting, solid = connected.

Wiring: every button between its GPIO and GND (internal pull-ups are used).
"""

import bluetooth
import binascii
import json
import os
import struct
from machine import Pin
from micropython import const
from time import sleep_ms, ticks_ms, ticks_diff

# ----------------------------------------------------------------- config ---
DEVICE_NAME = "ESP32 Gamepad"

# D-pad
PIN_UP, PIN_DOWN, PIN_LEFT, PIN_RIGHT = 32, 33, 25, 26

# Buttons: order defines the HID button number (bit 0 = button 1, ...)
BUTTON_PINS = {
    "A": 16,
    "B": 17,
    "X": 18,
    "Y": 19,
    "L": 21,
    "R": 22,
    "SELECT": 23,
    "START": 4,
}
BUTTON_ORDER = ("A", "B", "X", "Y", "L", "R", "SELECT", "START")

LED_PIN = 2
PAIR_COMBO_MS = 3000        # hold Start+Select this long to enter pairing mode
UNBONDED_TIMEOUT_MS = 30000  # drop connections that don't finish bonding
SECRETS_FILE = "bonds.json"

# ------------------------------------------------------------- BLE consts ---
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)

_FLAG_READ = const(0x0002)
_FLAG_WRITE_NO_RESPONSE = const(0x0004)
_FLAG_NOTIFY = const(0x0010)
_FLAG_READ_ENCRYPTED = const(0x0200)

_ADV_TYPE_FLAGS = const(0x01)
_ADV_TYPE_UUID16_COMPLETE = const(0x03)
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_APPEARANCE = const(0x19)

PAIRING = const(0)
GAMING = const(1)

# HID report descriptor: 8 buttons + 1 hat switch (2-byte report, no report ID)
HID_REPORT_MAP = bytes((
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

# Hat values: 0 = centered, 1 = N, 2 = NE, 3 = E, 4 = SE, 5 = S, 6 = SW, 7 = W, 8 = NW
# index bits: up=1, right=2, down=4, left=8
HAT_TABLE = {1: 1, 3: 2, 2: 3, 6: 4, 4: 5, 12: 6, 8: 7, 9: 8}

# -------------------------------------------------------------- hardware ---
led = Pin(LED_PIN, Pin.OUT)
dpad = [Pin(p, Pin.IN, Pin.PULL_UP) for p in (PIN_UP, PIN_RIGHT, PIN_DOWN, PIN_LEFT)]
buttons = [Pin(BUTTON_PINS[n], Pin.IN, Pin.PULL_UP) for n in BUTTON_ORDER]
BIT_SELECT = 1 << BUTTON_ORDER.index("SELECT")
BIT_START = 1 << BUTTON_ORDER.index("START")


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


# ----------------------------------------------------------- bond storage ---
secrets = {}


def load_secrets():
    try:
        with open(SECRETS_FILE) as f:
            for sec_type, key, value in json.load(f):
                secrets[(sec_type, binascii.a2b_base64(key))] = binascii.a2b_base64(value)
    except Exception:
        pass


def save_secrets():
    try:
        with open(SECRETS_FILE, "w") as f:
            json.dump(
                [(t, binascii.b2a_base64(k).decode(), binascii.b2a_base64(v).decode())
                 for (t, k), v in secrets.items()],
                f,
            )
    except Exception as e:
        print("save_secrets failed:", e)


def clear_bonds():
    secrets.clear()
    try:
        os.remove(SECRETS_FILE)
    except OSError:
        pass


# -------------------------------------------------------------- BLE setup ---
def _adv_field(adv_type, value):
    return struct.pack("BB", len(value) + 1, adv_type) + value


ADV_DATA = (
    _adv_field(_ADV_TYPE_FLAGS, b"\x06")
    + _adv_field(_ADV_TYPE_NAME, DEVICE_NAME.encode())
    + _adv_field(_ADV_TYPE_UUID16_COMPLETE, struct.pack("<H", 0x1812))
    + _adv_field(_ADV_TYPE_APPEARANCE, struct.pack("<H", 0x03C4))  # gamepad
)

UUID = bluetooth.UUID
SERVICES = (
    (
        UUID(0x1812),  # Human Interface Device
        (
            (UUID(0x2A4A), _FLAG_READ),                      # HID information
            (UUID(0x2A4B), _FLAG_READ),                      # Report map
            (UUID(0x2A4C), _FLAG_WRITE_NO_RESPONSE),         # Control point
            (
                UUID(0x2A4D),                                # Report (input)
                _FLAG_READ | _FLAG_READ_ENCRYPTED | _FLAG_NOTIFY,
                ((UUID(0x2908), _FLAG_READ),),               # Report reference
            ),
            (UUID(0x2A4E), _FLAG_READ | _FLAG_WRITE_NO_RESPONSE),  # Protocol mode
        ),
    ),
    (
        UUID(0x180A),  # Device Information
        (
            (UUID(0x2A29), _FLAG_READ),  # Manufacturer name
            (UUID(0x2A50), _FLAG_READ),  # PnP ID
        ),
    ),
    (
        UUID(0x180F),  # Battery
        ((UUID(0x2A19), _FLAG_READ | _FLAG_NOTIFY),),
    ),
)

ble = bluetooth.BLE()
ble.active(True)
ble.config(gap_name=DEVICE_NAME)
ble.config(bond=True, mitm=False, io=3)  # 3 = no input / no output ("Just Works")
try:
    ble.config(le_secure=True)
except Exception:
    pass

load_secrets()
mode = GAMING if secrets else PAIRING

conn = None            # current connection handle
conn_since = 0         # ticks when the current connection started
link_ready = False     # True once the link is encrypted AND bonded

((h_info, h_map, h_ctrl, h_report, h_report_ref, h_proto),
 (h_mfr, h_pnp),
 (h_batt,)) = ble.gatts_register_services(SERVICES)

ble.gatts_set_buffer(h_map, len(HID_REPORT_MAP))
ble.gatts_write(h_info, b"\x11\x01\x00\x02")            # HID 1.11, normally connectable
ble.gatts_write(h_map, HID_REPORT_MAP)
ble.gatts_write(h_report, b"\x00\x00")
ble.gatts_write(h_report_ref, b"\x00\x01")              # report ID 0, input report
ble.gatts_write(h_proto, b"\x01")                       # report protocol
ble.gatts_write(h_mfr, b"DIY")
ble.gatts_write(h_pnp, struct.pack("<BHHH", 0x02, 0x1209, 0x0001, 0x0100))
ble.gatts_write(h_batt, b"\x64")


def start_advertising():
    try:
        ble.gap_advertise(None)
        interval = 30_000 if mode == PAIRING else 100_000
        ble.gap_advertise(interval, adv_data=ADV_DATA, connectable=True)
    except OSError as e:
        print("advertise failed:", e)


def irq(event, data):
    global conn, conn_since, link_ready, mode

    if event == _IRQ_CENTRAL_CONNECT:
        conn, _addr_type, _addr = data
        conn_since = ticks_ms()
        link_ready = False

    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn = None
        link_ready = False
        start_advertising()

    elif event == _IRQ_ENCRYPTION_UPDATE:
        _handle, encrypted, _auth, bonded, _key_size = data
        link_ready = bool(encrypted and bonded)
        if link_ready and mode == PAIRING:
            mode = GAMING  # first device bonded -> lock to it
            print("Bonded. Switched to GAMING mode.")

    elif event == _IRQ_GET_SECRET:
        sec_type, index, key = data
        if key is None:
            i = 0
            for (t, _k), value in secrets.items():
                if t == sec_type:
                    if i == index:
                        return value
                    i += 1
            return None
        return secrets.get((sec_type, bytes(key)))

    elif event == _IRQ_SET_SECRET:
        sec_type, key, value = data
        key = (sec_type, bytes(key))
        if value is None:
            if key in secrets:
                del secrets[key]
                save_secrets()
                return True
            return False
        # In GAMING mode never learn keys from a new device.
        if mode == GAMING and key not in secrets:
            return False
        secrets[key] = bytes(value)
        save_secrets()
        return True


ble.irq(irq)


def enter_pairing_mode():
    global mode
    print("Entering PAIRING mode (old bond erased)")
    mode = PAIRING
    clear_bonds()
    if conn is not None:
        try:
            ble.gap_disconnect(conn)  # disconnect IRQ restarts advertising
        except OSError:
            start_advertising()
    else:
        start_advertising()


def send_report(bits, hat):
    if conn is None or not link_ready:
        return
    try:
        ble.gatts_write(h_report, bytes((bits, hat)))
        ble.gatts_notify(conn, h_report)
    except OSError:
        pass


# ------------------------------------------------------------- main loop ---
def update_led(now):
    if conn is not None and link_ready:
        led.value(1)
    elif mode == PAIRING:
        led.value((now // 100) % 2)          # fast blink
    else:
        led.value(1 if (now % 1500) < 100 else 0)  # short blink every 1.5 s


def main():
    global conn
    start_advertising()
    print("Started in", "PAIRING" if mode == PAIRING else "GAMING", "mode")

    state = (0, 0)
    candidate = state
    stable = 0
    combo_start = None
    combo_fired = False

    while True:
        now = ticks_ms()

        # Debounced input (state must be stable for 3 samples = ~15 ms)
        raw = read_inputs()
        if raw == candidate:
            stable += 1
        else:
            candidate, stable = raw, 0
        if stable >= 3 and candidate != state:
            state = candidate
            send_report(*state)

        # Start + Select held -> pairing mode
        if (state[0] & (BIT_START | BIT_SELECT)) == (BIT_START | BIT_SELECT):
            if combo_start is None:
                combo_start = now
            elif not combo_fired and ticks_diff(now, combo_start) >= PAIR_COMBO_MS:
                combo_fired = True
                enter_pairing_mode()
        else:
            combo_start, combo_fired = None, False

        # Drop connections that never complete bonding (unknown devices)
        if conn is not None and not link_ready:
            if ticks_diff(now, conn_since) > UNBONDED_TIMEOUT_MS:
                try:
                    ble.gap_disconnect(conn)
                except OSError:
                    pass

        update_led(now)
        sleep_ms(5)


main()