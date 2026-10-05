"""
Stage 4 HPFs (SPEC-06 §5.8, D22): the circles of the sites the pathologist confirmed in Stage 3.

HPF *i* is the circle of active site *i*: the site's centre, radius ``hpf_diameter_um / 2``. Nothing is
searched for and nothing moves to follow the figures, so the count is not biased by where they lie.
Sites change only in Stage 3. Circles never overlap (Stage 3 enforces it), so a candidate is in at most
one HPF; its padding (the site's frame) is imaged and swept, but a figure is counted only in the circle
that contains it. Fewer sites than ``k_max`` is a result (``hpf_count_lt_10``, pipeline/scoring.py).
"""
import math
from typing import Any, Dict, List, Optional

from pipeline.mitosis_gate import TumorGate
from pipeline.tissue_mask import TissueMask


class SiteWithoutCentreError(ValueError):
    """A confirmed hotspot has no ``center_um``: it was confirmed before HPF sites (WP-6.5); triage must run again."""


def hpfs_from_sites(
    sites: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    *,
    tissue: TissueMask,
    tumor: TumorGate,
    diameter_um: float,
) -> List[Dict[str, Any]]:
    """
    Stage 4's HPFs, one per site in the order given (``seq`` from 1).

    ``sites`` carry ``id``, ``center_um``, ``polygon_um`` (the frame) and ``source``; ``candidates`` carry
    ``centroid_um`` and ``counted``. Each HPF reports its ``count`` of counted candidates inside the circle,
    ``tissue_coverage`` and ``tumor_fraction`` (audit values), the ``hotspot_id`` and ``frame_um`` of its site.
    """
    if not sites:
        raise ValueError("HPFs are the confirmed hotspot sites; there are none")
    r = diameter_um / 2.0
    counted = [tuple(c["centroid_um"]) for c in candidates if c["counted"]]
    hpfs = []
    for seq, site in enumerate(sites, start=1):
        if site.get("center_um") is None:
            raise SiteWithoutCentreError(
                f"confirmed hotspot {site['id']!r} has no center_um; run triage again for this case"
            )
        cx, cy = (float(v) for v in site["center_um"])
        hpfs.append({
            "seq": seq,
            "center_um": [cx, cy],
            "radius_um": float(r),
            "count": sum(1 for p in counted if math.dist(p, (cx, cy)) <= r),
            "tissue_coverage": float(tissue.fraction_in_disk_um(cx, cy, r)),
            "tumor_fraction": float(tumor.tumor_fraction_in_disk(cx, cy, r)),
            "source": "model" if site.get("source", "model") == "model" else "pathologist",
            "hotspot_id": site["id"],
            "frame_um": [list(p) for p in site["polygon_um"]],
        })
    return hpfs


def hpf_seq_of(centroid_um, hpfs: List[Dict[str, Any]]) -> Optional[int]:
    """The ``seq`` of the HPF circle that contains the point, or None outside every circle."""
    for hpf in hpfs:
        cx, cy = hpf["center_um"]
        if math.dist(centroid_um, (cx, cy)) <= float(hpf["radius_um"]):
            return int(hpf["seq"])
    return None


def attach_sites(hpfs: List[Dict[str, Any]], hotspots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The HPFs with the ``hotspot_id`` and ``frame_um`` of the confirmed site at their centre.

    ``hotspots`` carry ``id``, ``center_um`` and ``polygon_um``. An HPF with no site at its centre raises: a
    stored HPF is always the circle of a confirmed site, and nothing is guessed in its place.
    """
    by_centre = {tuple(float(v) for v in h["center_um"]): h for h in hotspots if h.get("center_um") is not None}
    out = []
    for hpf in hpfs:
        site = by_centre.get(tuple(float(v) for v in hpf["center_um"]))
        if site is None:
            raise LookupError(f"HPF {hpf['seq']} at {hpf['center_um']} matches no confirmed hotspot site; run Stage 4 again")
        out.append({**hpf, "hotspot_id": site["id"], "frame_um": [list(p) for p in site["polygon_um"]]})
    return out
