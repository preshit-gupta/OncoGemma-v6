"""GET /api/v1/stages/grading/{case_id} matches docs/contracts/grading_v6.md exactly (WP-8.6).

The models mirror the contract's TypeScript types with ``extra="forbid"``, so a missing or an extra
field fails. The contract's example (the frontend mock fixture) must validate too.
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.main import app
from tests.test_grading_api import seed, setup_db_override  # noqa: F401 - the shared database fixture

REPO = Path(__file__).resolve().parents[2]
Score = Literal[1, 2, 3]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TubuleEstimate(Strict):
    tumor_present: bool
    tubule_percent: float


class TubuleReview(Strict):
    tumor_present: Optional[bool] = None
    tubule_percent: Optional[float] = None
    by: str
    at: str


class TubuleSample(Strict):
    id: str
    center_um: Tuple[float, float]
    size_um: Literal[512]
    mpp: Literal[1.0]
    image_url: str
    stratum: int
    tumor_area_um2: float
    estimate: Optional[TubuleEstimate]
    review: Optional[TubuleReview]


class PleoEstimate(Strict):
    pleomorphism_score: Score


class Nuclei(Strict):
    n: int
    area_p50_um2: float
    area_cv: float


class PleoReview(Strict):
    pleomorphism_score: Optional[Score] = None
    by: str
    at: str


class Verification(Strict):
    producer: str
    pleomorphism_score: Optional[Score]
    agrees: Optional[bool]


class PleoField(Strict):
    id: str
    center_um: Tuple[float, float]
    size_um: Literal[128]
    mpp: Literal[0.25]
    image_url: str
    stratum: int
    estimate: Optional[PleoEstimate]
    nuclei: Optional[Nuclei]
    review: Optional[PleoReview]
    verification: Optional[Verification] = None


class SlideGeom(Strict):
    width_px: int
    height_px: int
    mpp_x: float
    mpp_y: float


class Tubule(Strict):
    samples: List[TubuleSample]
    percent: Optional[float]
    score: Optional[Score]
    estimator: str
    n_used: int


class Pleomorphism(Strict):
    fields: List[PleoField]
    score: Optional[Score]
    estimator: str
    aggregation: Literal["mode", "p75", "model"]


class Mitotic(Strict):
    score: Optional[Score]
    count_total: int
    n_hpf: int
    area_mm2: float
    per_mm2: float
    # WP-6.5: the fixture gains these in WP-7.10; the API must always send them (asserted below).
    flags: List[Literal["hpf_count_lt_10"]] = []
    hpf_target: Optional[int] = None


class HistotypeVote(Strict):
    sample_id: str
    type: Optional[str]
    architecture: Optional[str]
    cohesion: Optional[str]
    confidence: Optional[Literal["low", "medium", "high"]]
    rationale: Optional[str]


class Histotype(Strict):
    type: Optional[str]
    estimator: str
    rationale: str
    confirmed: bool
    confirmed_by: Optional[str]
    agreement: Optional[float]
    n_requested: int
    votes: List[HistotypeVote]


class Overrides(Strict):
    tubule_score: Optional[Score] = None
    pleo_score: Optional[Score] = None
    histotype: Optional[str] = None
    reasons: Dict[str, str]


class Provenance(Strict):
    stage: str
    model_versions: Dict[str, str]
    config_hash: str
    run_mode: Literal["clinical", "eval", "shadow"]


class GradingStageV6(Strict):
    case_id: str
    stage_execution_id: str
    status: Literal["queued", "running", "awaiting_review", "confirmed", "failed"]
    slide: SlideGeom
    tubule: Tubule
    pleomorphism: Pleomorphism
    mitotic: Mitotic
    histotype: Histotype
    total: Optional[int]
    grade: Optional[Score]
    flags: List[Literal["needs_human", "insufficient_nuclei", "near_grade_boundary", "hpf_count_lt_10"]]
    overrides: Overrides
    provenance: Provenance


def test_get_matches_the_contract():
    client = TestClient(app)
    case_id = seed()
    body = client.get(f"/api/v1/stages/grading/{case_id}").json()
    GradingStageV6.model_validate(body)
    assert body["mitotic"]["hpf_target"] == 10 and body["mitotic"]["flags"] == []
    # After every kind of edit too.
    client.post("/api/v1/stages/grading/review-sample", json={"case_id": case_id, "kind": "pleo", "sample_id": "p_01",
                                                              "value": {"pleomorphism_score": 1}})
    client.post("/api/v1/stages/grading/review-sample", json={"case_id": case_id, "kind": "tubule", "sample_id": "t_01",
                                                              "value": {"tubule_percent": 12}})
    client.post("/api/v1/stages/grading/override", json={"case_id": case_id, "component": "tubule", "value": 3,
                                                         "reason": "solid sheets throughout"})
    body = client.post("/api/v1/stages/grading/histotype/confirm", json={"case_id": case_id, "type": "ILC"}).json()
    GradingStageV6.model_validate(body)


def test_the_contract_example_and_the_mock_fixture_validate():
    fixture = json.loads((REPO / "frontend/lib/mock/grading.json").read_text(encoding="utf-8"))
    GradingStageV6.model_validate(fixture)
    doc = (REPO / "docs/contracts/grading_v6.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)\n```", doc, re.S).group(1)
    GradingStageV6.model_validate(json.loads(example))
