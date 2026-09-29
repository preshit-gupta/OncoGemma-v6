"""The artifacts Stage 2 (preprocess) leaves for later stages: the registered tissue mask and the stain profile.

Worker tests seed them instead of running preprocess. The mask covers the same ellipse as the
``FakeOpenSlide`` section, so the registered tissue and the pixels agree.
"""
import numpy as np

from app.core.stain_profiles import save_stain_profile
from app.core.tissue_mask_store import save_tissue_mask
from app.models.case import Case
from pipeline.stain import StainFit
from pipeline.tissue_mask import TissueMask
from tests.fakes.he import W_HE

MASK_MPP = 8.0
REFERENCE_MAXC = [1.0, 0.8]


def ellipse_mask(width_um: float, height_um: float, mpp: float = MASK_MPP) -> TissueMask:
    """The FakeOpenSlide tissue section (an ellipse of 0.42 x 0.4 of the slide) as a registered mask."""
    cols, rows = int(np.ceil(width_um / mpp)), int(np.ceil(height_um / mpp))
    xs = (np.arange(cols) + 0.5) * mpp
    ys = (np.arange(rows) + 0.5) * mpp
    x, y = np.meshgrid(xs, ys)
    inside = ((x - width_um / 2) / (0.42 * width_um)) ** 2 + ((y - height_um / 2) / (0.4 * height_um)) ** 2 <= 1
    return TissueMask(inside, mpp, profile="resection")


def stain_fit(status: str = "fitted") -> StainFit:
    return StainFit(
        fitter_version="test", reference_id="test@v1",
        w_src=W_HE.tolist(), maxc_src=REFERENCE_MAXC, w_tgt=W_HE.tolist(), maxc_tgt=REFERENCE_MAXC,
        fit_status=status, n_patches=0 if status == "degenerate" else 30, mosaic_sha256="0" * 64,
    )


def seed_stage2(db, case_id, slide_id, width_um: float, height_um: float, *, degenerate: bool = False, specimen: str = "resection") -> None:
    """Give a seeded case its specimen type, registered tissue mask and stain profile."""
    case = db.get(Case, case_id)
    case.specimen_type = specimen
    save_tissue_mask(case_id, ellipse_mask(width_um, height_um))
    save_stain_profile(db, slide_id, stain_fit("degenerate" if degenerate else "fitted"))
    db.commit()
