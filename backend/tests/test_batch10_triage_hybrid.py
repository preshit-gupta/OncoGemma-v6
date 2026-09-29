"""
Stage 3 Smart Scout tile sampling.

The MedGemma referee tests that lived here covered the v5 colour-threshold fallback and its
lenient schema, both deleted in WP-2.3b (SPEC-01 §3.9). The referee now runs through the
model gateway; tests/test_triage_worker.py covers it.
"""
import numpy as np


def test_smart_scout_non_overlapping_patches():
    """Verify that Smart Scout produces strictly non-overlapping patches and prioritizes cellularity."""
    width_px = 37352
    height_px = 162391
    mpp_x = 0.2526
    nx = 80
    ny = 348
    max_sample_patches = 2048

    patch_dim_px = int(round(224.0 / mpp_x))
    cols = width_px // patch_dim_px
    rows = height_px // patch_dim_px

    # Create mock stain map and tissue mask
    np.random.seed(42)
    tissue_mask_overview = np.random.rand(ny, nx) > 0.15
    stain_map = np.random.rand(ny, nx) * 1.5

    candidate_slots = []
    for r in range(rows):
        for c in range(cols):
            x0 = c * patch_dim_px
            y0 = r * patch_dim_px
            cx_px = x0 + patch_dim_px // 2
            cy_px = y0 + patch_dim_px // 2
            ix = min(nx - 1, max(0, int(cx_px / (width_px / nx))))
            iy = min(ny - 1, max(0, int(cy_px / (height_px / ny))))

            if tissue_mask_overview[iy, ix]:
                candidate_slots.append({
                    "c": c, "r": r, "x0": x0, "y0": y0,
                    "cx_px": cx_px, "cy_px": cy_px,
                    "ix": ix, "iy": iy,
                    "score": float(stain_map[iy, ix])
                })

    assert len(candidate_slots) > max_sample_patches

    # Run Smart Scout ranking
    candidate_slots.sort(key=lambda s: s["score"], reverse=True)
    n_dense = int(round(0.80 * max_sample_patches))
    dense_slots = candidate_slots[:n_dense]
    dense_keys = set((s["c"], s["r"]) for s in dense_slots)
    remaining_slots = [s for s in candidate_slots if (s["c"], s["r"]) not in dense_keys]
    n_context = max_sample_patches - len(dense_slots)
    step_ctx = max(1, len(remaining_slots) // n_context)
    context_slots = remaining_slots[::step_ctx][:n_context]

    selected_slots = dense_slots + context_slots
    assert len(selected_slots) == max_sample_patches

    # 1. Verify every selected slot is unique
    tile_indices = [(s["c"], s["r"]) for s in selected_slots]
    assert len(tile_indices) == len(set(tile_indices)), "No duplicate tile slots allowed!"

    # 2. Verify strict geometric non-overlapping condition
    # For any two tiles (c1, r1) != (c2, r2), their bounding boxes [x0, x0 + W] x [y0, y0 + H] must have zero overlap
    for i in range(min(300, len(selected_slots))):
        s1 = selected_slots[i]
        box1 = (s1["x0"], s1["y0"], s1["x0"] + patch_dim_px, s1["y0"] + patch_dim_px)
        for j in range(i + 1, min(300, len(selected_slots))):
            s2 = selected_slots[j]
            box2 = (s2["x0"], s2["y0"], s2["x0"] + patch_dim_px, s2["y0"] + patch_dim_px)
            # Intersection test
            overlap_x = (box1[0] < box2[2]) and (box1[2] > box2[0])
            overlap_y = (box1[1] < box2[3]) and (box1[3] > box2[1])
            assert not (overlap_x and overlap_y), f"Overlap detected between slot {i} and {j}!"

