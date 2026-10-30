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
from typing import Dict

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
