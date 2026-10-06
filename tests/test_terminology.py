"""const.py is the single source of the type / confidence / severity vocabulary.

Every user-facing surface must use exactly those keys and words.
"""

import json
from pathlib import Path
import re

import yaml

from custom_components.downtime_auditor.const import (
    CONFIDENCE_LABELS,
    SEVERITY_LABELS,
    TYPE_LABELS,
    Confidence,
    FindingType,
    Severity,
)

PKG = Path(__file__).parent.parent / "custom_components" / "downtime_auditor"
STRINGS = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
PANEL = (PKG / "frontend" / "panel.js").read_text(encoding="utf-8")

# v0.4 words that must not come back on any user-facing surface.
RETIRED = re.compile(r"possibly[ _]missed|unverifiable|fired[ _]during[ _]startup", re.I)


def _panel_list(name: str) -> dict[str, str]:
    """key → label of a `const NAME = [ { key: ..., label: ... }, ... ]` list in panel.js."""
    block = re.search(rf"const {name} = \[(.*?)\];", PANEL, re.S)
    assert block, name
    return dict(re.findall(r'key: "([a-z_]+)", label: "([^"]+)"', block.group(1)))


def test_every_term_has_a_display_label():
    assert set(TYPE_LABELS) == set(FindingType)
    assert set(CONFIDENCE_LABELS) == set(Confidence)
    assert set(SEVERITY_LABELS) == set(Severity)


def test_strings_use_the_vocabulary():
    # Severity: entity states and the threshold selector (None is never a threshold).
    states = STRINGS["entity"]["sensor"]["highest_severity"]["state"]
    assert states == {s.value: SEVERITY_LABELS[s] for s in Severity}
    options = STRINGS["selector"]["severity"]["options"]
    assert options == {s.value: SEVERITY_LABELS[s] for s in Severity if s != Severity.NONE}
    # Types: one Repairs issue text per finding type.
    assert set(STRINGS["issues"]) == {t.value for t in FindingType}
    for issue in STRINGS["issues"].values():
        for placeholder in ("{severity}", "{severity_reason}", "{confidence}"):
            assert placeholder in issue["description"]


def test_translations_match_strings():
    en = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == STRINGS


def test_services_have_translations():
    services = yaml.safe_load((PKG / "services.yaml").read_text(encoding="utf-8"))
    assert set(services) == set(STRINGS["services"])


def test_panel_uses_the_vocabulary():
    assert _panel_list("TYPES") == {t.value: TYPE_LABELS[t] for t in FindingType}
    assert _panel_list("CONFIDENCES") == {c.value: CONFIDENCE_LABELS[c] for c in Confidence}
    assert _panel_list("SEVERITIES") == {s.value: SEVERITY_LABELS[s] for s in Severity}


def test_retired_terms_are_gone():
    surfaces = {
        "strings.json": json.dumps(STRINGS),
        "panel.js": PANEL,
        "services.yaml": (PKG / "services.yaml").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert not RETIRED.search(text), f"{name}: {RETIRED.search(text).group(0)}"
    # Python code may only name them where it reads v0.4 data (migration and history upgrade).
    allowed = {"__init__.py", "const.py", "report.py"}
    for path in PKG.glob("*.py"):
        if path.name not in allowed:
            assert not RETIRED.search(path.read_text(encoding="utf-8")), path.name
