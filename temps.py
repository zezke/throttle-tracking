"""Read Apple Silicon chip (die) temperatures via IOKit's HID event system.

powermetrics does not report temperatures on Apple Silicon, but the SoC exposes its
thermal sensors as HID services (usage page 0xff00, usage 5). The "PMU tdie*"
sensors are the on-die readings; "tdev" and battery sensors are ignored.
No root needed.

  python3 temps.py      # print all sensors and the die summary
"""
import ctypes
import ctypes.util

_V = ctypes.c_void_p
_UTF8 = 0x08000100
_TEMPERATURE_EVENT = 15
_client = None
_services = None


def _load():
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    cf.CFStringCreateWithCString.restype = _V
    cf.CFStringCreateWithCString.argtypes = [_V, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFNumberCreate.restype = _V
    cf.CFNumberCreate.argtypes = [_V, ctypes.c_int, _V]
    cf.CFDictionaryCreate.restype = _V
    cf.CFDictionaryCreate.argtypes = [_V, _V, _V, ctypes.c_long, _V, _V]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [_V]
    cf.CFArrayGetValueAtIndex.restype = _V
    cf.CFArrayGetValueAtIndex.argtypes = [_V, ctypes.c_long]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [_V, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [_V]
    io.IOHIDEventSystemClientCreate.restype = _V
    io.IOHIDEventSystemClientCreate.argtypes = [_V]
    io.IOHIDEventSystemClientSetMatching.argtypes = [_V, _V]
    io.IOHIDEventSystemClientCopyServices.restype = _V
    io.IOHIDEventSystemClientCopyServices.argtypes = [_V]
    io.IOHIDServiceClientCopyProperty.restype = _V
    io.IOHIDServiceClientCopyProperty.argtypes = [_V, _V]
    io.IOHIDServiceClientCopyEvent.restype = _V
    io.IOHIDServiceClientCopyEvent.argtypes = [_V, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
    io.IOHIDEventGetFloatValue.restype = ctypes.c_double
    io.IOHIDEventGetFloatValue.argtypes = [_V, ctypes.c_int32]
    return cf, io


_cf, _io = _load()


def _cfstr(s):
    return _cf.CFStringCreateWithCString(None, s.encode(), _UTF8)


def _cfint(i):
    v = ctypes.c_int32(i)
    return _cf.CFNumberCreate(None, 3, ctypes.byref(v))  # kCFNumberSInt32Type


def _sensor_services():
    """Return [(name, service)] for all temperature sensors, created once."""
    global _client, _services
    if _services:
        return _services
    keys = (_V * 2)(_cfstr("PrimaryUsagePage"), _cfstr("PrimaryUsage"))
    vals = (_V * 2)(_cfint(0xFF00), _cfint(5))
    kcb = _V.in_dll(_cf, "kCFTypeDictionaryKeyCallBacks")
    vcb = _V.in_dll(_cf, "kCFTypeDictionaryValueCallBacks")
    match = _cf.CFDictionaryCreate(None, keys, vals, 2, ctypes.addressof(kcb), ctypes.addressof(vcb))
    _client = _io.IOHIDEventSystemClientCreate(None)
    _io.IOHIDEventSystemClientSetMatching(_client, match)
    arr = _io.IOHIDEventSystemClientCopyServices(_client)
    product = _cfstr("Product")
    buf = ctypes.create_string_buffer(256)
    found = []
    for i in range(_cf.CFArrayGetCount(arr) if arr else 0):
        svc = _cf.CFArrayGetValueAtIndex(arr, i)
        name_ref = _io.IOHIDServiceClientCopyProperty(svc, product)
        if not name_ref:
            continue
        if _cf.CFStringGetCString(name_ref, buf, 256, _UTF8):
            found.append((buf.value.decode(), svc))
        _cf.CFRelease(name_ref)
    _services = found  # `arr` is kept alive on purpose: it owns the services
    return _services


def read_all():
    """Return {sensor name: °C} for every temperature sensor."""
    out = {}
    for name, svc in _sensor_services():
        ev = _io.IOHIDServiceClientCopyEvent(svc, _TEMPERATURE_EVENT, 0, 0)
        if not ev:
            continue
        out.setdefault(name, _io.IOHIDEventGetFloatValue(ev, _TEMPERATURE_EVENT << 16))
        _cf.CFRelease(ev)
    return out


def die_temps():
    """Return (average, maximum) on-die temperature in °C, or (None, None)."""
    try:
        vals = [t for n, t in read_all().items() if "tdie" in n and 0 < t < 130]
    except Exception:
        return None, None
    if not vals:
        return None, None
    return sum(vals) / len(vals), max(vals)


if __name__ == "__main__":
    for name, t in sorted(read_all().items()):
        print("%-28s %6.1f °C" % (name, t))
    avg, mx = die_temps()
    print("\nDie temperature: avg %.1f °C, max %.1f °C" % (avg, mx) if avg else "\nNo die sensors found")
