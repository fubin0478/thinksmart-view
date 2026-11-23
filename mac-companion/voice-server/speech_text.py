"""Spoken forms for numbers in English interpretation audio."""
import re

from num2words import num2words

_AMOUNT = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER = re.compile(r"(?<![\w.])(" + _AMOUNT + r")(?!\w|\.\d)")
_CURRENCY = re.compile(r"([$\u20ac\u00a3])\s*(" + _AMOUNT + r")(?!\w|\.\d)")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\.?)?\b", re.I)
_PROTECTED = re.compile(r"https?://\S+|\b\d+(?:\.\d+){2,}\b|\b\d{4}-\d{2}-\d{2}\b")


def _words(value):
    try:
        number = value.replace(',', '')
        if '.' in number:
            integer, fraction = number.split('.', 1)
            sign = 'minus ' if integer.startswith('-') else ''
            whole = num2words(int(integer.lstrip('+-')), lang='en')
            digits = ' '.join(num2words(int(digit), lang='en') for digit in fraction)
            return (sign + whole + ' point ' + digits).replace('-', ' ')
        return num2words(int(number), lang='en').replace('-', ' ')
    except (ValueError, OverflowError, NotImplementedError):
        return value


def _time_words(match):
    hour, minute = int(match[1]), int(match[2])
    if hour > 23 or minute > 59:
        return match[0]
    result = _words(str(hour))
    if minute == 0:
        result += " o'clock"
    elif minute < 10:
        result += ' oh ' + _words(str(minute))
    else:
        result += ' ' + _words(str(minute))
    if match[3]:
        result += ' in the morning' if match[3].lower() == 'a' else ' in the afternoon'
    return result


def _normalize_part(text):
    text = _TIME.sub(_time_words, text)
    def currency(match):
        value = match[2]
        unit = {'$': 'dollar', '\u20ac': 'euro', '\u00a3': 'pound'}[match[1]]
        plural = '' if value.replace(',', '') in {'1', '1.0', '1.00'} else 's'
        return _words(value) + ' ' + unit + plural
    text = _CURRENCY.sub(currency, text)
    text = _NUMBER.sub(lambda m: _words(m[1]), text)
    return re.sub(r'\s*%', ' percent', text)


def interpretation_speech_text(text, language):
    if language.lower() not in {'en', 'english'}:
        return text
    output = []
    start = 0
    for match in _PROTECTED.finditer(text):
        output.extend([_normalize_part(text[start:match.start()]), match[0]])
        start = match.end()
    output.append(_normalize_part(text[start:]))
    return ''.join(output)
