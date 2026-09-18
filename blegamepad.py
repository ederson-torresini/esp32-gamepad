"""
MicroPython BLE HID Gamepad example for ESP32 (NodeMCU-32S).

This uses a BLE HID Gamepad service so that browsers implementing the
Gamepad Web API (https://developer.mozilla.org/en-US/docs/Web/API/Gamepad_API)
can detect it once paired as a BLE HID device (via OS Bluetooth stack).

Steps performed:
 1. Enter BLE pairing/advertising mode (discoverable) using bonding + IO capability.
 2. Once a central (e.g. your laptop/phone/PC browser host) connects & pairs,
    the device exposes a standard HID Gamepad report descriptor.
 3. After pairing, we continuously send button/axis updates so the browser's
    Gamepad API (navigator.getGamepads()) reflects our virtual gamepad.

Wiring (optional, for physical testing):
  - Buttons on GPIO pins pulled up internally, active LOW when pressed.
  - Two potentiometers (or joystick module) on ADC pins for X/Y axes.

NOTE: Requires a MicroPython build with bluetooth (ubluetooth) support.
"""

import bluetooth
import struct
import time
from machine import Pin, ADC

# ---------------------------------------------------------------------------
# HID Report Descriptor - Standard Gamepad
# 8 buttons + 2 axes (X, Y), each axis 8-bit signed (-127..127)
# ---------------------------------------------------------------------------
_HID_REPORT_DESCRIPTOR = bytes(
    [
        0x05,
        0x01,  # Usage Page (Generic Desktop)
        0x09,
        0x05,  # Usage (Gamepad)
        0xA1,
        0x01,  # Collection (Application)
        0xA1,
        0x00,  #   Collection (Physical)
        # --- Buttons (8) ---
        0x05,
        0x09,  #   Usage Page (Button)
        0x19,
        0x01,  #   Usage Minimum (Button 1)
        0x29,
        0x08,  #   Usage Maximum (Button 8)
        0x15,
        0x00,  #   Logical Minimum (0)
        0x25,
        0x01,  #   Logical Maximum (1)
        0x75,
        0x01,  #   Report Size (1)
        0x95,
        0x08,  #   Report Count (8)
        0x81,
        0x02,  #   Input (Data, Var, Abs)
        # --- Axes X, Y ---
        0x05,
        0x01,  #   Usage Page (Generic Desktop)
        0x09,
        0x30,  #   Usage (X)
        0x09,
        0x31,  #   Usage (Y)
        0x15,
        0x81,  #   Logical Minimum (-127)
        0x25,
        0x7F,  #   Logical Maximum (127)
        0x75,
        0x08,  #   Report Size (8)
        0x95,
        0x02,  #   Report Count (2)
        0x81,
        0x02,  #   Input (Data, Var, Abs)
        0xC0,  #  End Collection (Physical)
        0xC0,  # End Collection (Application)
    ]
)

# BLE HID constants
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_ENCRYPTION_UPDATE = 28

_FLAG_READ = 0x0002
_FLAG_WRITE = 0x0008
_FLAG_NOTIFY = 0x0010
_FLAG_READ_ENCRYPTED = 0x0200

_HID_SERVICE_UUID = bluetooth.UUID(0x1812)
_HID_INFO_UUID = bluetooth.UUID(0x2A4A)
_HID_REPORT_MAP_UUID = bluetooth.UUID(0x2A4B)
_HID_CONTROL_POINT_UUID = bluetooth.UUID(0x2A4C)
_HID_REPORT_UUID = bluetooth.UUID(0x2A4D)
_HID_PROTOCOL_MODE_UUID = bluetooth.UUID(0x2A4E)

_REPORT_REF_DESC_UUID = bluetooth.UUID(0x2908)

_HID_INFO = struct.pack("<HBB", 0x0111, 0x00, 0x02)  # bcdHID, country code, flags

_HID_SERVICE = (
    _HID_SERVICE_UUID,
    (
        (_HID_INFO_UUID, _FLAG_READ),
        (_HID_REPORT_MAP_UUID, _FLAG_READ),
        (_HID_CONTROL_POINT_UUID, _FLAG_WRITE),
        (
            _HID_REPORT_UUID,
            _FLAG_READ | _FLAG_NOTIFY | _FLAG_READ_ENCRYPTED,
            ((_REPORT_REF_DESC_UUID, _FLAG_READ),),
        ),
        (_HID_PROTOCOL_MODE_UUID, _FLAG_READ | _FLAG_WRITE),
    ),
)

_BOARD_LED_PIN = 2


class BLEGamepad:
    def __init__(self, name="ESP32-Gamepad"):
        self._led = Pin(_BOARD_LED_PIN, Pin.OUT)
        self._led.off()
        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)

        # IO capability: no input/output -> "Just Works" pairing.
        self._ble.config(bond=True)
        self._ble.config(le_secure=True)
        self._ble.config(mitm=False)
        # Older MicroPython builds do not expose the IO capability constants.
        # NimBLE's numeric value for "no input/output" is 3.
        self._ble.config(io=getattr(bluetooth, "IO_CAPABILITY_NO_INPUT_OUTPUT", 3))

        (
            (
                self._h_info,
                self._h_report_map,
                self._h_control,
                self._h_report,
                self._h_report_ref,
                self._h_protocol,
            ),
        ) = self._ble.gatts_register_services((_HID_SERVICE,))

        self._ble.gatts_write(self._h_info, _HID_INFO)
        self._ble.gatts_write(self._h_report_map, _HID_REPORT_DESCRIPTOR)
        # Report Reference descriptor: report id 0, report type Input (1)
        self._ble.gatts_write(self._h_report_ref, struct.pack("<BB", 0, 1))
        self._ble.gatts_write(self._h_protocol, b"\x01")

        self._connections = set()
        self._name = name
        self._advertise()

        # Current gamepad state.
        self._buttons = 0
        self._axis_x = 0
        self._axis_y = 0

    # -----------------------------------------------------------------
    # BLE event handling
    # -----------------------------------------------------------------
    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            if self._connections:
                self._ble.gap_disconnect(conn_handle)
                print("Rejected additional central:", conn_handle)
                return

            self._connections.add(conn_handle)
            self._ble.gap_advertise(None)
            self._led.on()
            print("Central connected:", conn_handle)

        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self._connections.discard(conn_handle)
            self._led.value(bool(self._connections))
            print("Central disconnected:", conn_handle)
            self._advertise()

        elif event == _IRQ_GATTS_WRITE:
            conn_handle, attr_handle = data
            print("Write on handle:", attr_handle)

        elif event == _IRQ_ENCRYPTION_UPDATE:
            conn_handle, encrypted, authenticated, bonded, key_size = data
            print(
                "Encryption update - conn:",
                conn_handle,
                "encrypted:",
                encrypted,
                "authenticated:",
                authenticated,
                "bonded:",
                bonded,
            )

    def _advertise(self, interval_us=250000):
        # Advertise with HID appearance (Gamepad = 0x03C4) so hosts can
        # identify device type during pairing/scan.
        name_bytes = self._name.encode()
        adv_payload = self._build_adv_payload(name_bytes)
        self._ble.gap_advertise(interval_us, adv_data=adv_payload)
        print("Advertising as '%s' - enter pairing mode on your host now." % self._name)

    @staticmethod
    def _build_adv_payload(name_bytes):
        payload = bytearray()

        def _append(adv_type, value):
            payload.extend(struct.pack("BB", len(value) + 1, adv_type) + value)

        # Flags: general discoverable, BR/EDR not supported.
        _append(0x01, struct.pack("B", 0x06))
        # Appearance: HID Gamepad (0x03C4).
        _append(0x19, struct.pack("<H", 0x03C4))
        # Complete list of 16-bit service UUIDs: HID service (0x1812).
        _append(0x03, struct.pack("<H", 0x1812))
        # Complete local name.
        _append(0x09, name_bytes)

        return bytes(payload)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def is_connected(self):
        return len(self._connections) > 0

    def set_button(self, index, pressed):
        """index: 0-7"""
        if pressed:
            self._buttons |= 1 << index
        else:
            self._buttons &= ~(1 << index)

    def set_axes(self, x, y):
        """x, y expected in range -127..127"""
        self._axis_x = max(-127, min(127, x))
        self._axis_y = max(-127, min(127, y))

    def send_report(self):
        if not self._connections:
            return
        report = struct.pack("<Bbb", self._buttons & 0xFF, self._axis_x, self._axis_y)
        for conn_handle in self._connections:
            try:
                self._ble.gatts_notify(conn_handle, self._h_report, report)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Example usage: read physical buttons/joystick and stream to connected host
# ---------------------------------------------------------------------------


def _map_adc_to_signed(adc_value, in_min=0, in_max=4095):
    """Map a 12-bit ADC reading (0-4095) to signed range -127..127."""
    centered = adc_value - (in_max // 2)
    scale = 127 / (in_max // 2)
    return int(max(-127, min(127, centered * scale)))


def main():
    gamepad = BLEGamepad(name="ESP32-Gamepad")

    # --- Optional physical controls ---
    NUM_BUTTONS = 8
    button_pins = []
    for gpio in (13, 12, 14, 27, 26, 25, 33, 32):
        p = Pin(gpio, Pin.IN, Pin.PULL_UP)
        button_pins.append(p)

    adc_x = ADC(Pin(34))
    adc_x.atten(ADC.ATTN_11DB)
    adc_y = ADC(Pin(35))
    adc_y.atten(ADC.ATTN_11DB)

    print("Waiting for BLE pairing... put your host in pairing/scan mode.")

    while True:
        if gamepad.is_connected():
            for i in range(NUM_BUTTONS):
                pressed = button_pins[i].value() == 0  # active low
                gamepad.set_button(i, pressed)

            x = _map_adc_to_signed(adc_x.read())
            y = _map_adc_to_signed(adc_y.read())
            gamepad.set_axes(x, y)

            gamepad.send_report()

        time.sleep_ms(20)  # ~50 Hz report rate


if __name__ == "__main__":
    main()
