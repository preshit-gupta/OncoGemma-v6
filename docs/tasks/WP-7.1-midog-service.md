# WP-7.1 — MIDOG detector service: v2 contract (separate repo)

> **Status: done (2026-09-29/30).** MIDOG-microservice `c67ccf3`, follow-ups #1 (real weights hash, `min_prob`) and #2 (image coordinates; tiatoolbox 2.0.1 returned x/y transposed). Deployed as Vertex model 831662348912558080 (`v2-d788edf`, T4); positive control F1 0.851 (v2). **Deviation:** `input_mpp` is 0.25 µm/px by measurement, not the 0.5 µm/px of the TIAToolbox IO config (F1 0.87 vs 0.27; D17). The SPEC-06 §4 wording "verify from the IO config" needs an owner amendment. See `docs/IMPLEMENTATION_PLAN.md` §2.2.

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | S | SPEC-06 §4 | — | E (repo `D:\Projects\MIDOG`, remote `preshit-gupta/MIDOG-microservice`) |

## Goal

Upgrade the Vertex AI mitosis-detector container to the v6 contract:
- the resolution is checked;
- input is lossless;
- raw probabilities are returned;
- weights are pinned and reported;
- the YOLO fallback is removed.

The **legacy request format must keep working** until the OncoGemma client switches (WP-7.2).

## Read first (only these)

- In the OncoGemma repo: `docs/specs/06-stage4-mitosis.md` §4 and `AGENTS.md` (the rules apply here too)
- In the MIDOG repo: `main.py`, `Dockerfile`, `requirements.txt`, `deploy.ps1`, `test_predict.py`

## Files you may touch (MIDOG repo only)

- `main.py`, `Dockerfile`, `requirements.txt`, `deploy.ps1`, `README.md`, `test_predict.py`
- Create `engine.py`, `tests/test_contract.py` and `requirements-dev.txt`
- Delete `best.pt` and `scripts/setup_weights.py`'s YOLO bootstrap path

## Contract to implement

```
GET  /health    -> 200 {"status":"healthy","model":"KongNet_Det_MIDOG_1","weights_sha256":"<sha>"} | 503 if not loaded
GET  /metadata  -> 200 {"model":"KongNet_Det_MIDOG_1","tiatoolbox":"<version>","weights_sha256":"<sha>",
                        "input_mpp":<float>,"patch_px":512,"output":"points","deterministic":true,"contract":"v2"}
POST /predict   (Vertex AI predict route; body {"instances":[...], "parameters":{...}})
```

The v2 and legacy formats are told apart **per instance**.

| Instance shape | Behaviour |
|---|---|
| **v2:** `{"image_png_b64": str, "mpp": float}` plus optional `parameters.min_prob` (default 0.01) | Decode as PNG, and reject non-PNG input with a per-instance error. Reject `abs(mpp − input_mpp)/input_mpp > 0.01` with `{"points": [], "error": "mpp_mismatch: expected <x>, got <y>"}`. Reject images that are not `patch_px × patch_px`. Otherwise return `{"points": [{"x": f, "y": f, "prob": f}, ...], "error": null}` for detections with `prob ≥ min_prob`. `x, y` are pixel coordinates in the input image |
| **legacy:** `{"image_bytes": str, "confidence_threshold"?: float}` | Unchanged v5 behaviour, keeping the `boxes` response shape, except that there is no YOLO branch. Log `legacy_contract_used`. |

The response top level always includes `"model_sha256": "<sha>"`.

## Tasks

1. **`engine.py`.** Wrap TIAToolbox `NucleusDetector(model="KongNet_Det_MIDOG_1")` as `class KongNetEngine: predict(np.ndarray RGB) -> list[(x, y, prob)]`.
   - Determine `input_mpp` from the model's pretrained IO config (inspect the TIAToolbox pretrained-model `ioconfig` for this model) and log it at startup.
   - If it cannot be determined programmatically, use env `MODEL_INPUT_MPP` (default `0.25`), and say so in the PR.
2. **Weights hash.** At startup, locate the cached weights file TIAToolbox downloaded and compute its SHA-256.
   - The Dockerfile's pre-warm step must also write that hash to `/app/WEIGHTS_SHA256`.
   - Serve it in `/health`, `/metadata` and responses.
3. **Determinism.** Call `torch.use_deterministic_algorithms(True, warn_only=True)`. Set cuDNN `benchmark = False`. Use a fixed batch size.
4. **Remove YOLO entirely:**
   - `MODEL_ENGINE` handling
   - `predict_yolo`
   - the `ultralytics` requirement
   - the `best.pt` copy in the Dockerfile
   - the `deploy.ps1` `-Engine yolo` path
5. **Dependencies.** Pin `tiatoolbox==<the version currently resolved>` and the other requirements to exact versions.
6. **Tests** (`tests/test_contract.py`, FastAPI `TestClient`). Inject a `FakeEngine` through a module-level factory, so the tests need neither TIAToolbox nor a GPU. Cover:
   - `/metadata` fields;
   - the v2 happy path;
   - `mpp_mismatch`;
   - wrong size;
   - non-PNG input;
   - `min_prob` filtering;
   - the legacy path returning `boxes`;
   - `/health` returning 503 when the engine failed to load.
7. **README and `test_predict.py`.** Document v2 with a sample request, and make the smoke-test script send v2 requests.

## Acceptance (run in the MIDOG repo)

```powershell
pip install -r requirements-dev.txt     # fastapi, httpx, pytest, pillow, numpy (no tiatoolbox needed for tests)
python -m pytest tests -q
```

Optional, with GCP access: build the image and run `test_predict.py` against a local container, pasting `/metadata` output into the PR.

## Out of scope — do not do

- Deploying to Vertex AI. The program owner deploys after merging.
- The nuclei segmentation route (WP-8.3).
- Any OncoGemma repo change.

## Done checklist

- [ ] v2 contract implemented; legacy preserved; YOLO removed
- [ ] `input_mpp` determined (or env fallback documented) and weights SHA-256 reported
- [ ] Contract tests pass with `FakeEngine`
- [ ] README and smoke script updated
