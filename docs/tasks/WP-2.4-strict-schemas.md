# WP-2.4 — Strict model-output schemas

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | S | SPEC-01 §3.5 | — | D |

## Goal

Create the v6 strict Pydantic schemas for every model output, plus a strict JSON parser. **Pre-written tests define the behaviour:** `backend/tests/inference/test_strict_schemas.py` (currently skipped; it activates when your module exists).

## Read first (only these)

- `docs/specs/01-measurement-integrity.md` §1.2 and §3.5
- `backend/tests/inference/test_strict_schemas.py`
- `AGENTS.md`

## Files you may touch

- Create `backend/app/inference/__init__.py` (empty) and `backend/app/inference/schemas.py`.
- Nothing else. **Do not edit `backend/pipeline/medgemma.py`.** Claude wires the new schemas into call sites in WP-2.3 and later WPs.

## Interfaces

```python
# backend/app/inference/schemas.py
class SchemaInvalidError(ValueError):
    def __init__(self, model_name: str, detail: str): ...
    model_name: str            # e.g. "PleoEstimate"
    # str(err) must include the pydantic error text (field names)

class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

class TumorVerdict(StrictModel):
    tumor_present: bool
    lesion_type: Literal["invasive_carcinoma", "in_situ", "benign_stroma", "inflammation", "adipose"]
    rationale: str = ""

class MitosisCriteria(StrictModel):
    membrane_absent: bool
    condensed_chromosome_projections: bool
    phase: Literal["prometaphase", "metaphase", "anaphase", "telophase", "atypical", "none"]
    neoplastic_cell: bool

class MitosisVerdict(StrictModel):
    verdict: Literal["MITOTIC_FIGURE", "NOT_MITOTIC_FIGURE", "EQUIVOCAL"]
    criteria: MitosisCriteria
    mimic: Literal["none", "apoptotic_body", "pyknotic_nucleus", "hyperchromatic_interphase",
                   "lymphocyte", "prophase", "crush", "other"]
    rationale: str = ""

class TubuleEstimate(StrictModel):
    tumor_present: bool
    tubule_percent: int = Field(ge=0, le=100)
    rationale: str = ""

class PleoEstimate(StrictModel):
    pleomorphism_score: Literal[1, 2, 3]
    rationale: str = ""

class HistotypeVerdict(StrictModel):
    type: Literal["IDC-NST", "ILC", "mixed_ductal_lobular", "mucinous", "tubular", "papillary",
                  "micropapillary", "metaplastic", "other"]
    rationale: str = ""

def parse_json_strict(model_cls: type[StrictModel], raw: str) -> StrictModel: ...
```

## Rules

- **The only normalisation allowed** is on enum/`Literal` string fields: trim whitespace, then do a **case-insensitive exact match** against the members and return the canonical member. Use `field_validator(..., mode="before")`, applied only when the input is a `str`. Anything else must fail validation.
- **No coercion:** no `str → bool`, `str → int` or `float → int`, and no substring matching (the v5 bug: `"NOT CONFIRMED"` contained `"CONFIRM"`).
- **Pydantic trap:** even in strict mode, `Literal[1, 2, 3]` accepts `2.0` and `True`. Add a `mode="before"` validator on `pleomorphism_score` that raises unless `isinstance(v, int) and not isinstance(v, bool)`. The test `test_pleo_invalid[payload4]` checks this.
- **No defaults** on decision fields. Only `rationale` defaults to `""`.
- **`parse_json_strict` accepts** only:
  - surrounding whitespace;
  - a single optional Markdown code fence, either ```` ```json ```` or ```` ``` ````.

  Anything else (prose, trailing commas, JSON repair) raises `SchemaInvalidError`. `json.JSONDecodeError` and `ValidationError` are both wrapped into `SchemaInvalidError(model_name=model_cls.__name__, ...)`.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/inference -q -p no:cacheprovider     # all pass, none skipped
python -m pytest backend/tests -q -p no:cacheprovider               # nothing else broke
```

## Out of scope — do not do

- Do not change any call site, prompt or `pipeline/medgemma.py`.
- Do not add JSON-repair logic.

## Done checklist

- [ ] `app/inference/schemas.py` implements the interface
- [ ] `backend/tests/inference` passes with 0 skipped
- [ ] Full suite green
