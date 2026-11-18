from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

INTENT = "intent.yaml"
INTENT_MARKDOWN = "intent.md"
EXAMPLES = "examples"
SCENARIOS = "scenarios"
PROPERTIES = "properties.yaml"
FEATURES = "features"
DRIVER = "driver"
MANIFEST = "manifest.lock.json"
SPECS_ROOT = ".factory/specs"


@dataclass(frozen=True)
class SpecLayout:
    root: Path

    @classmethod
    def for_feature(cls, repo: Path, feature_id: str) -> SpecLayout:
        return cls(repo / SPECS_ROOT / feature_id)

    @property
    def intent(self) -> Path:
        return self.root / INTENT

    @property
    def intent_markdown(self) -> Path:
        return self.root / INTENT_MARKDOWN

    @property
    def examples(self) -> Path:
        return self.root / EXAMPLES

    @property
    def scenarios(self) -> Path:
        return self.root / SCENARIOS

    @property
    def properties(self) -> Path:
        return self.root / PROPERTIES

    @property
    def features(self) -> Path:
        return self.root / FEATURES

    @property
    def driver(self) -> Path:
        return self.root / DRIVER

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST
