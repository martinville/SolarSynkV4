import re


MAX_DEVICE_NAME_LENGTH = 80
DEVICE_NAME_HINT = 'Use 1–80 letters, numbers, spaces, hyphens or underscores. Start and end with a letter or number.'
_DEVICE_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,78}[A-Za-z0-9])?\Z')


def validate_device_name(value):
    name = str(value).strip()
    if not _DEVICE_NAME.fullmatch(name):
        raise ValueError(DEVICE_NAME_HINT)
    return name


def device_name(saved, alias, serial):
    """Use only safe display names, including when reading older saved data."""
    for candidate in (saved, alias, serial):
        try:
            return validate_device_name(candidate or '')
        except ValueError:
            continue
    return 'Inverter'
