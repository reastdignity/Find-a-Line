"""
The texts of the web interface on the server: the message catalogue locales/<language>.json, the
same file the page bundles (viewer/src/i18n.ts, i18next). Keys are flat and namespaced
("field.radius.label", "choice.nmpt.off", "topic.lines", "preset.long"); text() formats them as
i18next does: `{{name}}` takes the value of a parameter (stays as it is without one) and a `count`
picks the plural form "<key>_zero" (0), "<key>_one" or "<key>_other" where the catalogue has one.
An unknown key gives the key itself (as i18next). English only for now; the command line keeps its
own texts (params.py descriptions, its messages).
"""

import json
import os
import re
from functools import lru_cache
from typing import Dict, Optional

LOCALES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
LANGUAGE = "en"
_VARIABLE = re.compile(r"\{\{-?\s*([^{}]+?)\s*\}\}")  # {{name}}, {{ name }}, {{- name}} (unescaped)


@lru_cache(maxsize=None)
def catalogue(language: str = LANGUAGE) -> Dict[str, str]:
    """The texts of `language`, read once."""
    with open(os.path.join(LOCALES, f"{language}.json"), encoding="utf-8") as f:
        return json.load(f)


def _plural(count) -> str:
    """The plural category of English (Intl.PluralRules("en")): "one" for 1, "other" for the rest."""
    return "one" if count == 1 else "other"


def _string(value) -> str:
    """A value as i18next writes it (JavaScript's String(), None as ""): 2.0 as "2", True as "true"."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def text(key: str, language: str = LANGUAGE, **params) -> str:
    """The text of `key` with `params` filled in; the key itself when the catalogue has none."""
    return render(catalogue(language), key, **params)


def render(texts: Dict[str, str], key: str, **params) -> str:
    """text() from the catalogue `texts` (tests give their own samples)."""
    keys = [key]
    count = params.get("count")
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        keys.insert(0, f"{key}_{_plural(count)}")
        if count == 0:
            keys.insert(0, f"{key}_zero")
    found = next((texts[k] for k in keys if k in texts), None)
    if found is None:
        return key
    return _VARIABLE.sub(lambda m: _string(params[m.group(1)]) if m.group(1) in params else m.group(0), found)


# --- Coded errors and notes: one text for the command line (option names, as before) and one for the
# web page, from the catalogue ("error.<code>"), with the same parameters on the server and in the page
# (viewer/src/i18n.ts errorText). A parameter "field" (a field of params.SearchParams) gives the text its
# label ("label") in the page's language. ---

def web_params(params: Dict, language: str = LANGUAGE) -> Dict:
    """`params` with the label of their "field" added (as the page's errorText does)."""
    field = params.get("field")
    if field and "label" not in params:
        params = {**params, "label": text(f"field.{field}.label", language)}
    return params


class Coded:
    """What a coded error or note adds to its class: `code` (the key "error.<code>" of the catalogue,
    None: no web text of its own, str() is shown) and `params` (JSON values)."""
    code: Optional[str] = None
    params: Dict = {}

    def web_text(self, language: str = LANGUAGE) -> str:
        """The text for the web page (str() without a code)."""
        if not self.code:
            return str(self)
        return text(f"error.{self.code}", language, **web_params(self.params, language))

    def info(self) -> Optional[Dict]:
        """{"code", "params"} for the page, None without a code."""
        return {"code": self.code, "params": self.params} if self.code else None

    def detail(self):
        """The detail of an HTTP error answer: {"message": the English web text, "code", "params"}, or
        the plain text without a code."""
        if not self.code:
            return str(self)
        return {"message": self.web_text(), "code": self.code, "params": self.params}


def _coded(instance: Coded, code: Optional[str], params: Dict):
    instance.code, instance.params = code, params


class UserError(Coded, ValueError):
    """A value or request the user can change: `cli` is the text of the command line (str()), `code`
    and `params` give the web page's ("error.<code>"); UserError.web(code, ...) has the web text as
    its str() too."""

    def __init__(self, cli: str, code: Optional[str] = None, **params):
        super().__init__(cli)
        _coded(self, code, params)

    @classmethod
    def web(cls, code: str, **params) -> "UserError":
        return cls(text(f"error.{code}", **web_params(params)), code, **params)


class ServiceError(Coded, RuntimeError):
    """A RuntimeError of a download or a service with a web text of its own (see UserError)."""

    def __init__(self, cli: str, code: Optional[str] = None, **params):
        super().__init__(cli)
        _coded(self, code, params)


class Note(str):
    """A message of a search (on_event's text, e.g. a warning) with a web text: the str is the
    command line's text, `code` and `params` the page's (as Coded)."""
    code: Optional[str]
    params: Dict

    def __new__(cls, cli: str, code: Optional[str] = None, **params):
        note = super().__new__(cls, cli)
        note.code, note.params = code, params
        return note

    web_text = Coded.web_text
    info = Coded.info


# pydantic's errors of a value it cannot read: the code of their web text
_PYDANTIC = {"float_parsing": "not_a_number", "float_type": "not_a_number", "finite_number": "not_a_number",
             "int_parsing": "not_whole_number", "int_type": "not_whole_number", "int_from_float": "not_whole_number",
             "bool_parsing": "not_yes_no", "bool_type": "not_yes_no", "literal_error": "not_a_choice",
             "string_type": "not_a_text", "missing": "missing_value"}


def user_error(error: BaseException) -> Optional[Coded]:
    """The coded error in `error`: itself, or the first error of a pydantic ValidationError (the coded
    error a validator raised, or one for the field pydantic could not read); None for other errors."""
    if isinstance(error, Coded):
        return error
    errors = getattr(error, "errors", None)
    if not callable(errors) or type(error).__name__ != "ValidationError":
        return None
    try:
        first = errors()[0]
    except (IndexError, TypeError):
        return None
    inner = (first.get("ctx") or {}).get("error")
    if isinstance(inner, Coded):
        return inner
    if isinstance(inner, BaseException):  # a validator's error without a code: its text
        return UserError(str(inner))
    loc = [part for part in first.get("loc", ()) if isinstance(part, str)]
    field = loc[0] if loc else None
    code = _PYDANTIC.get(first.get("type"), "invalid_value")
    params = {"field": field} if field and f"field.{field}.label" in catalogue() else {}
    if not params:
        code = "invalid_request"
    return UserError(f"{'.'.join(map(str, first.get('loc', ())))}: {first.get('msg')}", code, **params)


def quantity(field: str, value) -> str:
    """A value of a field with its unit, as the texts write it: "2000 m", "45°", "5 %", "50"."""
    unit = catalogue().get(f"field.{field}.unit", "")
    number = _string(float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
    if not unit:
        return number
    return f"{number}{unit}" if unit == "°" else f"{number} {unit}"
