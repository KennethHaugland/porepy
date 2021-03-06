NANO = 1e-9
MICRO = 1e-6
MILLI = 1e-3
CENTI = 1e-2
DECI = 1e-1
KILO = 1e3
MEGA = 1e6
GIGA = 1e9

# Time units
SECOND = 1.
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR
YEAR = 365 * DAY

KILOGRAM = 1.
GRAM = 1e-3 * KILOGRAM

METER = 1.
CENTIMETER = CENTI * METER
MILLIMETER = MILLI * METER
KILOMETER = KILO * METER

# Pressure related quantities
DARCY = 9.869233e-13
MILLIDARCY = MILLI * DARCY

PASCAL = 1.
BAR = 101325 * PASCAL

CELSIUS = 1.

GRAVITY_ACCELERATION = 9.80665 * METER / SECOND ** 2
ATMOSPHERIC_PRESSURE = BAR


def CELSIUS_to_KELVIN(celsius):
    return celsius + 273.15


def KELKIN_to_CELSIUS(kelvin):
    return kelvin - 273.15


# force
NEWTON = KILOGRAM * METER / SECOND ** 2
