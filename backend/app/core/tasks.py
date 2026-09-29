"""Closed catalogues for decision records (SPEC-01 §3.3)."""
from enum import Enum


class Task(str, Enum):
    """What a decision is about. Every DecisionRecord names exactly one task."""

    PF_EMBED = "pf_embed"
    TUMOR_HEAD = "tumor_head"
    TUMOR_REFEREE = "tumor_referee"
    HOTSPOT_SELECT = "hotspot_select"
    MITOSIS_DETECT = "mitosis_detect"
    MITOSIS_CLASSIFY = "mitosis_classify"
    MITOSIS_REFEREE = "mitosis_referee"
    MITOSIS_COUNT = "mitosis_count"
    TUBULE_PATCH = "tubule_patch"
    TUBULE_SLIDE = "tubule_slide"
    PLEO_FIELD = "pleo_field"
    PLEO_SLIDE = "pleo_slide"
    HISTOTYPE = "histotype"
    GRADE_AGGREGATE = "grade_aggregate"
    HUMAN_EDIT = "human_edit"


class EntityType(str, Enum):
    """What a decision was made about. ``*_batch`` records list their entities in a sidecar."""

    TILE_BATCH = "tile_batch"
    TILE = "tile"
    HOTSPOT = "hotspot"
    CANDIDATE = "candidate"
    PATCH = "patch"
    FIELD = "field"
    SLIDE = "slide"

    @property
    def is_batch(self) -> bool:
        return self.value.endswith("_batch")


class ProducerKind(str, Enum):
    MODEL = "model"
    HEURISTIC = "heuristic"
    HUMAN = "human"
    FALLBACK = "fallback"
    SHADOW = "shadow"


class DecisionStatus(str, Enum):
    OK = "ok"
    SCHEMA_INVALID = "schema_invalid"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    SKIPPED = "skipped"
