"""Strict output models for non-VLM predictions (SPEC-01 §3.5). VLM answers live in ``schemas.py``.

A model's ``decision_output()`` is what its DecisionRecord stores when the full output
is too bulky for the table.
"""
import hashlib
import math
from typing import Annotated, Any

import numpy as np
from pydantic import Field, model_validator

from app.inference.schemas import StrictModel

NonEmptyVector = Annotated[list[float], Field(min_length=1)]


class EmbeddingBatch(StrictModel):
    """(N, D) embeddings, one row per input image in request order."""

    embeddings: Annotated[list[NonEmptyVector], Field(min_length=1)]

    @model_validator(mode="after")
    def _rectangular_and_finite(self) -> "EmbeddingBatch":
        dims = {len(row) for row in self.embeddings}
        if len(dims) != 1:
            raise ValueError(f"embedding rows have different lengths {sorted(dims)}")
        if not all(math.isfinite(value) for row in self.embeddings for value in row):
            raise ValueError("embeddings contain NaN or infinity")
        return self

    def as_array(self) -> np.ndarray:
        return np.asarray(self.embeddings, dtype=np.float32)

    def decision_output(self) -> dict[str, Any]:
        array = self.as_array()
        return {
            "n": int(array.shape[0]),
            "dim": int(array.shape[1]),
            "float32_sha256": hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest(),
        }


Probability = Annotated[float, Field(ge=0, le=1)]


class ClassProbabilities(StrictModel):
    """``predict_proba`` rows of a classifier, with its class labels in column order."""

    classes: Annotated[list[int | str], Field(min_length=2)]
    probabilities: Annotated[list[list[Probability]], Field(min_length=1)]

    @model_validator(mode="after")
    def _rows_are_distributions(self) -> "ClassProbabilities":
        if len(set(self.classes)) != len(self.classes):
            raise ValueError(f"duplicate class labels {self.classes}")
        for index, row in enumerate(self.probabilities):
            if len(row) != len(self.classes):
                raise ValueError(f"row {index} has {len(row)} columns for {len(self.classes)} classes")
            if not math.isclose(sum(row), 1.0, abs_tol=1e-6):
                raise ValueError(f"row {index} sums to {sum(row)}, not 1")
        return self

    def column(self, label: int | str) -> np.ndarray:
        """The probability of class ``label`` for every row. Unknown labels raise ValueError."""
        return np.asarray([row[self.classes.index(label)] for row in self.probabilities], dtype=np.float32)
