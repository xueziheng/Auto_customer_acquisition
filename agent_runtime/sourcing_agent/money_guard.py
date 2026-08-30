"""模型输出中的金额文本确定性分类边界。"""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from dataclasses import dataclass

_MAX_TEXT_CHARS = 16_000
_MAX_TRUSTED_LITERALS = 100
_MAX_TRUSTED_LITERAL_CHARS = 4_000
_MAX_TOTAL_TRUSTED_CHARS = 200_000
_SENTENCE_BOUNDARIES = frozenset(".?!;。！？；\r\n\u2028\u2029")
_DECIMAL_SEPARATORS = frozenset(".,．，٫")
_IDENTITY_WORDS = frozenset({"model", "series", "grade", "type", "part", "sku", "code"})
_IDENTITY_NUMBER_WORDS = frozenset({"no", "number"})
_PRICE_WORDS = frozenset(
    {"price", "prices", "priced", "pricing", "cost", "costs", "costing"}
)
_ISO_4217_CODES = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BOV",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHE",
        "CHF",
        "CHW",
        "CLF",
        "CLP",
        "CNY",
        "COP",
        "COU",
        "CRC",
        "CUC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MXV",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SLL",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "USN",
        "UYI",
        "UYU",
        "UYW",
        "UZS",
        "VED",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XAG",
        "XAU",
        "XBA",
        "XBB",
        "XBC",
        "XBD",
        "XCD",
        "XDR",
        "XOF",
        "XPD",
        "XPF",
        "XPT",
        "XSU",
        "XTS",
        "XUA",
        "YER",
        "ZAR",
        "ZMW",
        "ZWG",
        "ZWL",
    ]
)
_CURRENCY_CODES = _ISO_4217_CODES | {"RMB"}
_PRICE_UNITS = frozenset(
    {
        "bag",
        "bags",
        "bottle",
        "bottles",
        "box",
        "boxes",
        "carton",
        "cartons",
        "case",
        "cases",
        "drum",
        "drums",
        "g",
        "gram",
        "grams",
        "kg",
        "kilogram",
        "kilograms",
        "l",
        "liter",
        "liters",
        "litre",
        "litres",
        "m",
        "meter",
        "meters",
        "metre",
        "metres",
        "ml",
        "pack",
        "packs",
        "pair",
        "pairs",
        "pallet",
        "pallets",
        "pc",
        "pcs",
        "piece",
        "pieces",
        "roll",
        "rolls",
        "set",
        "sets",
        "sheet",
        "sheets",
        "sqm",
        "ton",
        "tons",
        "tonne",
        "tonnes",
        "unit",
        "units",
    }
)


@dataclass(frozen=True, slots=True)
class _NumberToken:
    start: int
    end: int
    canonical: str
    is_decimal: bool


@dataclass(frozen=True, slots=True)
class _WordToken:
    start: int
    end: int
    normalized: str


@dataclass(frozen=True, slots=True)
class _Sentence:
    start: int
    end: int
    numbers: tuple[_NumberToken, ...]
    words: tuple[_WordToken, ...]
    currency_symbols: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _SpanCoverage:
    starts: tuple[int, ...]
    prefix_max_ends: tuple[int, ...]

    @classmethod
    def from_spans(cls, spans: tuple[tuple[int, int], ...]) -> _SpanCoverage:
        starts: list[int] = []
        prefix_max_ends: list[int] = []
        maximum_end = -1
        for start, end in spans:
            starts.append(start)
            maximum_end = max(maximum_end, end)
            prefix_max_ends.append(maximum_end)
        return cls(tuple(starts), tuple(prefix_max_ends))

    def covers(self, span: tuple[int, int]) -> bool:
        index = bisect_right(self.starts, span[0]) - 1
        return index >= 0 and self.prefix_max_ends[index] >= span[1]


def _is_valid_bounded_text(value: object, *, maximum: int) -> bool:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs" or category == "Cf":
            return False
        if category == "Cc" and character not in "\r\n":
            return False
    return True


def _decimal_value(character: str) -> int | None:
    try:
        return unicodedata.decimal(character)
    except (TypeError, ValueError):
        return None


def _is_decimal_separator(text: str, index: int) -> bool:
    if text[index] not in _DECIMAL_SEPARATORS or index + 1 >= len(text):
        return False
    if _decimal_value(text[index + 1]) is None:
        return False
    return (
        index == 0
        or _decimal_value(text[index - 1]) is not None
        or not text[index - 1].isalnum()
    )


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(text):
        if character not in _SENTENCE_BOUNDARIES:
            continue
        if character == "." and _is_decimal_separator(text, index):
            continue
        if start < index:
            spans.append((start, index))
        start = index + 1
    if start < len(text):
        spans.append((start, len(text)))
    return tuple(spans)


def _lex_sentence(text: str, start: int, end: int) -> _Sentence:
    numbers: list[_NumberToken] = []
    words: list[_WordToken] = []
    currency_symbols: list[int] = []
    cursor = start
    while cursor < end:
        character = text[cursor]
        if unicodedata.category(character) == "Sc":
            currency_symbols.append(cursor)
            cursor += 1
            continue
        digit = _decimal_value(character)
        leading_decimal = (
            character in _DECIMAL_SEPARATORS
            and cursor + 1 < end
            and _decimal_value(text[cursor + 1]) is not None
            and (cursor == start or _decimal_value(text[cursor - 1]) is None)
        )
        if digit is not None or leading_decimal:
            number_start = cursor
            canonical: list[str] = []
            is_decimal = False
            if leading_decimal:
                canonical.append(".")
                is_decimal = True
                cursor += 1
            else:
                while cursor < end:
                    value = _decimal_value(text[cursor])
                    if value is None:
                        break
                    canonical.append(str(value))
                    cursor += 1
                if (
                    cursor < end
                    and text[cursor] in _DECIMAL_SEPARATORS
                    and cursor + 1 < end
                    and _decimal_value(text[cursor + 1]) is not None
                ):
                    canonical.append(".")
                    is_decimal = True
                    cursor += 1
            while cursor < end:
                value = _decimal_value(text[cursor])
                if value is None:
                    break
                canonical.append(str(value))
                cursor += 1
            numbers.append(
                _NumberToken(
                    start=number_start,
                    end=cursor,
                    canonical="".join(canonical),
                    is_decimal=is_decimal,
                )
            )
            continue
        if character.isalpha():
            word_start = cursor
            cursor += 1
            while cursor < end and text[cursor].isalpha():
                cursor += 1
            words.append(
                _WordToken(
                    start=word_start,
                    end=cursor,
                    normalized=unicodedata.normalize(
                        "NFKC", text[word_start:cursor]
                    ).casefold(),
                )
            )
            continue
        cursor += 1
    return _Sentence(
        start=start,
        end=end,
        numbers=tuple(numbers),
        words=tuple(words),
        currency_symbols=tuple(currency_symbols),
    )


def _literal_spans(text: str, literals: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for literal in literals:
        if not literal:
            continue
        cursor = text.find(literal)
        while cursor >= 0:
            spans.append((cursor, cursor + len(literal)))
            cursor = text.find(literal, cursor + 1)
    return tuple(sorted(spans))


def _covered(span: tuple[int, int], allowed: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in allowed)


def _spans_within_sentences(
    spans: tuple[tuple[int, int], ...],
    sentences: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    bounded: list[tuple[int, int]] = []
    sentence_index = 0
    for span in spans:
        while (
            sentence_index < len(sentences) and sentences[sentence_index][1] <= span[0]
        ):
            sentence_index += 1
        if sentence_index >= len(sentences):
            break
        sentence_start, sentence_end = sentences[sentence_index]
        if sentence_start <= span[0] and span[1] <= sentence_end:
            bounded.append(span)
    return tuple(bounded)


def _identity_spans(text: str, sentence: _Sentence) -> tuple[tuple[int, int], ...]:
    ordered: list[_WordToken | _NumberToken] = sorted(
        (*sentence.words, *sentence.numbers), key=lambda item: item.start
    )
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(ordered):
        if not isinstance(token, _WordToken) or token.normalized not in _IDENTITY_WORDS:
            continue
        cursor = index + 1
        prefix_end = token.end
        optional_number_word = ordered[cursor] if cursor < len(ordered) else None
        if (
            isinstance(optional_number_word, _WordToken)
            and optional_number_word.normalized in _IDENTITY_NUMBER_WORDS
        ):
            if not _between_is_identity_separator(
                text, prefix_end, optional_number_word.start
            ):
                continue
            prefix_end = optional_number_word.end
            cursor += 1
        if cursor >= len(ordered):
            continue
        payload = ordered[cursor]
        if isinstance(payload, _NumberToken):
            following = ordered[cursor + 1] if cursor + 1 < len(ordered) else None
            if (
                isinstance(following, _WordToken)
                and following.normalized.upper() in _CURRENCY_CODES
                and _between_is_identity_separator(text, prefix_end, payload.start)
                and _between_is_currency_identity_separator(
                    text, payload.end, following.start
                )
            ):
                spans.append((token.start, following.end))
            elif _between_is_identity_separator(text, prefix_end, payload.start):
                spans.append((token.start, payload.end))
            continue
        if (
            isinstance(payload, _WordToken)
            and payload.normalized.upper() in _CURRENCY_CODES
            and cursor + 1 < len(ordered)
            and isinstance(ordered[cursor + 1], _NumberToken)
            and _between_is_identity_separator(text, prefix_end, payload.start)
            and _between_is_identity_separator(
                text, payload.end, ordered[cursor + 1].start
            )
        ):
            spans.append((token.start, ordered[cursor + 1].end))
            continue
        if (
            isinstance(payload, _WordToken)
            and cursor + 1 < len(ordered)
            and isinstance(ordered[cursor + 1], _NumberToken)
            and payload.end == ordered[cursor + 1].start
            and _between_is_identity_separator(text, prefix_end, payload.start)
        ):
            spans.append((token.start, ordered[cursor + 1].end))
    return tuple(spans)


def _currency_word_tokens(sentence: _Sentence) -> tuple[_WordToken, ...]:
    return tuple(
        word for word in sentence.words if word.normalized.upper() in _CURRENCY_CODES
    )


def _price_word_tokens(sentence: _Sentence) -> tuple[_WordToken, ...]:
    return tuple(word for word in sentence.words if word.normalized in _PRICE_WORDS)


def _between_is_phrase_separator(text: str, left: int, right: int) -> bool:
    return all(
        character.isspace() or unicodedata.category(character) == "Pd"
        for character in text[left:right]
    )


def _between_is_identity_separator(text: str, left: int, right: int) -> bool:
    return _between_is_phrase_separator(text, left, right)


def _between_is_currency_identity_separator(text: str, left: int, right: int) -> bool:
    return all(
        character == "/"
        or character.isspace()
        or unicodedata.category(character) == "Pd"
        for character in text[left:right]
    )


def _has_per_unit_amount(text: str, sentence: _Sentence) -> bool:
    ordered: list[_WordToken | _NumberToken] = sorted(
        (*sentence.words, *sentence.numbers), key=lambda item: item.start
    )
    for index, token in enumerate(ordered):
        if not isinstance(token, _NumberToken) or index + 1 >= len(ordered):
            continue
        following = ordered[index + 1]
        if not isinstance(following, _WordToken):
            continue
        if (
            following.normalized in _PRICE_UNITS
            and text[token.end : following.start].strip() == "/"
        ):
            return True
        if following.normalized == "per" and index + 2 < len(ordered):
            unit = ordered[index + 2]
            if isinstance(unit, _WordToken) and unit.normalized in _PRICE_UNITS:
                return True
    return False


def _exempt_price_phrase_ends(
    text: str, sentence: _Sentence
) -> tuple[tuple[_WordToken, int], ...]:
    words = sentence.words
    exemptions: list[tuple[_WordToken, int]] = []
    for index, word in enumerate(words):
        if index + 1 >= len(words):
            continue
        next_word = words[index + 1]
        if not _between_is_phrase_separator(text, word.end, next_word.start):
            continue
        if (
            word.normalized == "cost"
            and next_word.normalized
            in {
                "impact",
                "implication",
            }
            or word.normalized == "price"
            and next_word.normalized == "sensitive"
        ):
            exemptions.append((word, next_word.end))
    return tuple(exemptions)


def _currency_pairs_are_identity_bound(
    text: str,
    sentence: _Sentence,
    currencies: tuple[_WordToken, ...],
    identities: tuple[tuple[int, int], ...],
) -> bool:
    for currency in currencies:
        if not any(
            start <= currency.start
            and currency.end <= end
            and any(
                start <= number.start and number.end <= end
                for number in sentence.numbers
            )
            for start, end in identities
        ):
            return False
    outside_words = tuple(
        word.normalized
        for word in sentence.words
        if not _covered((word.start, word.end), identities)
    )
    outside_numbers = [
        number
        for number in sentence.numbers
        if not _covered((number.start, number.end), identities)
    ]
    if outside_numbers:
        return False
    if outside_words:
        return False
    for index, character in enumerate(text[sentence.start : sentence.end]):
        absolute = sentence.start + index
        if _covered((absolute, absolute + 1), identities):
            continue
        if character.isalpha() or character.isspace():
            continue
        if unicodedata.category(character) != "Pd":
            return False
    return True


def _sentence_contains_untrusted_money(
    text: str,
    sentence: _Sentence,
    trusted_coverage: _SpanCoverage,
) -> bool:
    if sentence.currency_symbols:
        return True
    if _has_per_unit_amount(text, sentence):
        return True
    identities = _identity_spans(text, sentence)
    currencies = _currency_word_tokens(sentence)
    if (
        currencies
        and sentence.numbers
        and not _currency_pairs_are_identity_bound(
            text, sentence, currencies, identities
        )
    ):
        return True

    price_words = _price_word_tokens(sentence)
    if price_words and sentence.numbers:
        exemptions = _exempt_price_phrase_ends(text, sentence)
        exempt_words = {word.start for word, _ in exemptions}
        if any(word.start not in exempt_words for word in price_words):
            return True
        if (
            ":" in text[sentence.start : sentence.end]
            or "=" in text[sentence.start : sentence.end]
        ):
            return True
        if any(
            not (
                trusted_coverage.covers((number.start, number.end))
                or _covered((number.start, number.end), identities)
            )
            for number in sentence.numbers
        ):
            return True
        for _, phrase_end in exemptions:
            for number in sentence.numbers:
                if phrase_end <= number.start and not _covered(
                    (number.start, number.end), identities
                ):
                    return True

    for number in sentence.numbers:
        if number.is_decimal and not trusted_coverage.covers(
            (number.start, number.end)
        ):
            return True
    return False


def contains_untrusted_money(
    text: str,
    *,
    trusted_literals: tuple[str, ...],
) -> bool:
    """判断模型文本是否包含未受可信规格原文约束的金额表达。"""

    if not _is_valid_bounded_text(text, maximum=_MAX_TEXT_CHARS):
        return True
    if (
        not isinstance(trusted_literals, tuple)
        or len(trusted_literals) > _MAX_TRUSTED_LITERALS
    ):
        return True
    total_trusted_chars = 0
    for literal in trusted_literals:
        if not _is_valid_bounded_text(literal, maximum=_MAX_TRUSTED_LITERAL_CHARS):
            return True
        total_trusted_chars += len(literal)
        if total_trusted_chars > _MAX_TOTAL_TRUSTED_CHARS:
            return True

    unique_literals = tuple(dict.fromkeys(trusted_literals))
    sentence_spans = _sentence_spans(text)
    literal_spans = _literal_spans(text, unique_literals)
    trusted_coverage = _SpanCoverage.from_spans(
        _spans_within_sentences(literal_spans, sentence_spans)
    )
    for start, end in sentence_spans:
        sentence = _lex_sentence(text, start, end)
        if _sentence_contains_untrusted_money(text, sentence, trusted_coverage):
            return True
    return False
