"""BCSS masks on the 224 µm tile grid (SPEC-05 §4.1; WP-6.2)."""
import numpy as np
import pytest
from training.tumor_head.labels import (
    UnknownMaskCodeError,
    group_table,
    label_roi_tiles,
    label_space,
    load_config,
)

CODES = {0: "outside_roi", 1: "tumor", 2: "stroma", 3: "lymphocytic_infiltrate", 7: "exclude", 9: "fat",
         13: "normal_acinus_or_duct", 16: "nerve", 20: "dcis", 4: "necrosis_or_debris", 6: "blood",
         10: "plasma_cells", 11: "other_immune_infiltrate", 15: "undetermined", 21: "other"}
TILE_UM = 224.0
MPP = 1.0  # one mask pixel per µm, so a tile is 224 x 224 mask pixels


@pytest.fixture(scope="module")
def space():
    return label_space(load_config())


def _label(mask, space, origin=(0, 0), mpp=MPP):
    return label_roi_tiles(mask, origin, (mpp, mpp), TILE_UM, CODES, space).set_index(["i", "j"])


def test_repo_config_maps_every_spec_class(space):
    assert space.classes == ("invasive_tumor", "in_situ", "benign_epithelium", "stroma", "inflammatory", "necrosis",
                             "adipose_background")
    assert space.positive_class == "invasive_tumor"
    assert space.class_map["tumor"] == "invasive_tumor" and space.class_map["dcis"] == "in_situ"


def test_unmapped_codes_get_their_own_groups_and_unannotated_codes_none(space):
    lut, names = group_table(CODES, space)
    assert lut[0] == -1 and lut[7] == -1
    assert names[lut[1]] == "invasive_tumor"
    assert names[lut[16]] == "unmapped:nerve"


def test_a_pure_tile_takes_its_class(space):
    mask = np.full((224, 448), 1, np.uint16)
    mask[:, 224:] = 2
    tiles = _label(mask, space)
    assert tiles.loc[(0, 0), "label"] == "invasive_tumor"
    assert tiles.loc[(1, 0), "label"] == "stroma"
    assert tiles.loc[(0, 0), "annotated_fraction"] == pytest.approx(1.0)


def test_tiles_follow_the_global_grid_from_the_slide_origin(space):
    # ROI origin at slide pixel (300, 0): its first 148 columns fall in tile 1, the rest in tile 2.
    mask = np.full((224, 448), 1, np.uint16)
    tiles = _label(mask, space, origin=(300, 0))
    assert sorted(tiles.index.get_level_values("i")) == [1, 2, 3]
    assert tiles.loc[(1, 0), "annotated_fraction"] == pytest.approx(148 / 224)
    assert tiles.loc[(2, 0), "label"] == "invasive_tumor"


def test_too_little_annotation_is_excluded(space):
    mask = np.zeros((224, 224), np.uint16)
    mask[:, :100] = 1  # 100/224 < 0.5 of the tile annotated
    tile = _label(mask, space).loc[(0, 0)]
    assert tile["label"] is None and tile["exclusion"] == "few_annotated"


def test_exclude_code_counts_as_not_annotated(space):
    mask = np.full((224, 224), 7, np.uint16)
    mask[:, :120] = 1
    tile = _label(mask, space).loc[(0, 0)]
    assert tile["annotated_fraction"] == pytest.approx(120 / 224)
    assert tile["label"] == "invasive_tumor"


def test_no_majority_is_excluded(space):
    mask = np.full((224, 224), 1, np.uint16)
    mask[:, 75:150] = 2
    mask[:, 150:] = 9  # three classes of about a third each
    tile = _label(mask, space).loc[(0, 0)]
    assert tile["label"] is None and tile["exclusion"] == "minor_majority"


def test_merged_codes_vote_together(space):
    mask = np.full((224, 224), 3, np.uint16)  # lymphocytes
    mask[:, 80:150] = 10  # plasma cells -> inflammatory too
    mask[:, 150:] = 2
    assert _label(mask, space).loc[(0, 0), "label"] == "inflammatory"


def test_unmapped_majority_is_excluded_not_guessed(space):
    mask = np.full((224, 224), 16, np.uint16)  # nerve
    tile = _label(mask, space).loc[(0, 0)]
    assert tile["label"] is None and tile["exclusion"] == "unmapped_majority"


def test_resolution_scales_the_tile_area(space):
    # At 0.5 µm/px a 224 µm tile is 448 mask pixels wide.
    mask = np.full((448, 448), 20, np.uint16)
    tiles = _label(mask, space, mpp=0.5)
    assert list(tiles.index) == [(0, 0)]
    assert tiles.loc[(0, 0), "label"] == "in_situ"


def test_unknown_mask_code_raises(space):
    with pytest.raises(UnknownMaskCodeError):
        _label(np.full((10, 10), 99, np.uint16), space)
