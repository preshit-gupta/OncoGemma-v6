"""Shared constrained types for the typed pipeline configuration (SPEC-01 §3.8)."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Coarsest resolution any stage reads, in µm/px (SPEC-01 §3.8: confloat(gt=0, le=2) on mpp).
MAX_MPP = 2.0
# Coarsest resolution of a whole-slide overview (tissue mask, QC), in µm/px (SPEC-04 §3.2, §3.6).
MAX_OVERVIEW_MPP = 16.0


class StrictModel(BaseModel):
    """Unknown keys are errors, values are not coerced across types, and instances are immutable."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0, le=1)]
Percent = Annotated[float, Field(ge=0, le=100)]
Mpp = Annotated[float, Field(gt=0, le=MAX_MPP)]
OverviewMpp = Annotated[float, Field(gt=0, le=MAX_OVERVIEW_MPP)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RegistryKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
