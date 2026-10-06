"""Country dialling codes for the phone field, and the one normaliser both
forms use to build a stored WhatsApp number.

Why this is shared rather than duplicated in the two templates: the WhatsApp
number is the *login identifier*, matched as an exact string against the
``users`` row. If ``/enroll`` stored ``+91 98765 43210`` and ``/login`` later
submitted ``+919876543210``, the lookup would miss and the user would be told
their password was wrong. :func:`normalize_whatsapp` is therefore the single
place that turns a dropdown selection plus a typed number into the stored form,
and both routes call it.

Numbers are stored in E.164: a leading ``+``, then digits only, 15 max.
"""
from __future__ import annotations

#: ``(iso, dial_code, name)``. The dial code carries no ``+``.
#:
#: Not every country on earth — a curated list covering India plus the places
#: Indian investors most often live. Adding one is a single tuple; the forms and
#: the validator pick it up automatically.
COUNTRIES: tuple[tuple[str, str, str], ...] = (
    ("IN", "91", "India"),
    ("AE", "971", "United Arab Emirates"),
    ("AU", "61", "Australia"),
    ("BD", "880", "Bangladesh"),
    ("BH", "973", "Bahrain"),
    ("CA", "1", "Canada"),
    ("CH", "41", "Switzerland"),
    ("CN", "86", "China"),
    ("DE", "49", "Germany"),
    ("ES", "34", "Spain"),
    ("FR", "33", "France"),
    ("GB", "44", "United Kingdom"),
    ("HK", "852", "Hong Kong"),
    ("ID", "62", "Indonesia"),
    ("IE", "353", "Ireland"),
    ("IL", "972", "Israel"),
    ("IT", "39", "Italy"),
    ("JP", "81", "Japan"),
    ("KE", "254", "Kenya"),
    ("KW", "965", "Kuwait"),
    ("LK", "94", "Sri Lanka"),
    ("MU", "230", "Mauritius"),
    ("MY", "60", "Malaysia"),
    ("NG", "234", "Nigeria"),
    ("NL", "31", "Netherlands"),
    ("NP", "977", "Nepal"),
    ("NZ", "64", "New Zealand"),
    ("OM", "968", "Oman"),
    ("PH", "63", "Philippines"),
    ("PK", "92", "Pakistan"),
    ("PL", "48", "Poland"),
    ("QA", "974", "Qatar"),
    ("RU", "7", "Russia"),
    ("SA", "966", "Saudi Arabia"),
    ("SE", "46", "Sweden"),
    ("SG", "65", "Singapore"),
    ("TH", "66", "Thailand"),
    ("TR", "90", "Turkey"),
    ("US", "1", "United States"),
    ("VN", "84", "Vietnam"),
    ("ZA", "27", "South Africa"),
)

#: Preselected in both forms.
DEFAULT_DIAL_CODE = "91"

#: Dial codes the forms will accept, so a hand-rolled POST cannot store junk.
#: A set because ``+1`` is shared by the US and Canada.
VALID_DIAL_CODES: frozenset[str] = frozenset(dial for _iso, dial, _name in COUNTRIES)

_E164_MAX_DIGITS = 15  # ITU-T E.164
_MIN_NATIONAL_DIGITS = 4


def options() -> list[dict[str, str]]:
    """The dropdown entries, India first and the rest alphabetical.

    Returned as dicts so the template does not index tuples. Both ``name`` and
    ``label`` are shown: a bare ``+1`` would not say whether it means Canada or
    the United States, which share the code.
    """
    ordered = sorted(COUNTRIES, key=lambda c: (c[1] != DEFAULT_DIAL_CODE, c[2]))
    return [
        {"iso": iso, "dial": dial, "name": name, "label": f"+{dial}"}
        for iso, dial, name in ordered
    ]


def normalize_whatsapp(dial_code: str, national_number: str) -> str:
    """Combine a dropdown selection and a typed number into a stored E.164 string.

    Punctuation and spaces are dropped, so ``98765 43210`` and ``98765-43210``
    cannot become two different accounts. A leading zero is dropped too: it is a
    domestic trunk prefix in most of the world (UK ``07…`` dials as ``+447…``)
    and no E.164 subscriber number begins with one.

    Raises ``ValueError`` with a message meant for the user.
    """
    dial = (dial_code or "").strip().lstrip("+")
    if dial not in VALID_DIAL_CODES:
        raise ValueError("Pick a country code from the list.")

    digits = "".join(ch for ch in (national_number or "") if ch.isdigit())
    digits = digits.lstrip("0")

    if not digits:
        raise ValueError("Enter your WhatsApp number.")
    if len(digits) < _MIN_NATIONAL_DIGITS:
        raise ValueError("That WhatsApp number is too short.")
    if len(dial) + len(digits) > _E164_MAX_DIGITS:
        raise ValueError("That WhatsApp number is too long.")

    return f"+{dial}{digits}"


def split_whatsapp(stored: str) -> tuple[str, str]:
    """Inverse of :func:`normalize_whatsapp`, for repopulating a form after an error.

    Falls back to the default code and the raw digits when nothing matches, so a
    rejected form still shows the user roughly what they typed. Longest dial
    code wins, so ``+971`` is not read as ``+97``.
    """
    digits = "".join(ch for ch in (stored or "") if ch.isdigit())
    for dial in sorted(VALID_DIAL_CODES, key=len, reverse=True):
        if digits.startswith(dial):
            return dial, digits[len(dial):]
    return DEFAULT_DIAL_CODE, digits
