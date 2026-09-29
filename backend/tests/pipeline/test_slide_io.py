"""SlideReader and read_region_at_mpp (SPEC-04 §3.1, AC6 and AC7).

Level selection, resampling and the ICC flag run against a procedural pyramid; geometry across
resolutions and thread safety run against real pyramidal TIFFs through the OpenSlide library.
"""
import hashlib
import threading
import uuid

import numpy as np
import openslide
import pytest
from PIL import Image, ImageCms

from pipeline.errors import IccProfileError, MissingMppError, RegionOutOfBoundsError, SlideReadError
from pipeline.slide_io import (
    MAX_READ_PIXELS,
    SlideReader,
    centered_origin_um,
    clamp_origin_um,
    read_region_at_mpp,
)
from tests.fakes.slide import FakeOpenSlide
from tests.fakes.tiff import halve, tissue_rgb, write_pyramid_tiff

MPP = 0.25


def open_fake(monkeypatch, slide: FakeOpenSlide, mpp: float = MPP) -> SlideReader:
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return SlideReader("fake.svs", mpp, mpp, "svs")


# -- the reader -------------------------------------------------------------------------------


def test_levels_carry_the_row_resolution_scaled_by_each_downsample(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(4096, 2048, downsamples=(1, 4, 16)))
    assert [(lv.index, lv.downsample, lv.mpp_x, lv.width_px, lv.height_px) for lv in reader.levels] == [
        (0, 1.0, 0.25, 4096, 2048),
        (1, 4.0, 1.0, 1024, 512),
        (2, 16.0, 4.0, 256, 128),
    ]
    assert reader.extent_um() == (1024.0, 512.0)
    assert reader.native_mpp == 0.25


def test_the_resolution_comes_from_the_caller_not_the_file(monkeypatch):
    slide = FakeOpenSlide(1000, 1000)
    slide.properties = {openslide.PROPERTY_NAME_MPP_X: "0.5", openslide.PROPERTY_NAME_MPP_Y: "0.5"}
    reader = open_fake(monkeypatch, slide, mpp=0.25)
    assert reader.native_mpp == 0.25
    assert reader.extent_um() == (250.0, 250.0)


@pytest.mark.parametrize("mpp", [0, -0.25, float("nan"), float("inf")])
def test_a_reader_refuses_an_unusable_resolution(monkeypatch, mpp):
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(100, 100))
    with pytest.raises(ValueError, match="mpp_x"):
        SlideReader("fake.svs", mpp, 0.25, "svs")


def test_from_slide_row_needs_a_recorded_resolution(monkeypatch):
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(100, 100))

    class Row:
        id, format, mpp_x, mpp_y = uuid.uuid4(), "svs", None, None

    with pytest.raises(MissingMppError, match="needs_mpp"):
        SlideReader.from_slide_row("fake.svs", Row())
    Row.mpp_x = Row.mpp_y = 0.5
    assert SlideReader.from_slide_row("fake.svs", Row()).native_mpp == 0.5


def test_an_unopenable_file_fails_at_construction(monkeypatch):
    def refuse(path):
        raise openslide.OpenSlideError("Unsupported or missing image file")

    monkeypatch.setattr(openslide, "OpenSlide", refuse)
    with pytest.raises(SlideReadError, match="could not open slide"):
        SlideReader("broken.svs", MPP, MPP, "svs")


def test_a_closed_reader_refuses_reads(monkeypatch):
    slide = FakeOpenSlide(512, 512)
    reader = open_fake(monkeypatch, slide)
    reader.close()
    assert slide.closed
    with pytest.raises(SlideReadError, match="closed"):
        read_region_at_mpp(reader, 0, 0, 32, 32, MPP)


@pytest.mark.parametrize(
    "downsamples, message",
    [((2, 4), "level 0 must have downsample 1"), ((1, 4, 2), "must increase")],
)
def test_a_malformed_pyramid_is_refused(monkeypatch, downsamples, message):
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(1024, 1024, downsamples=downsamples))
    with pytest.raises(SlideReadError, match=message):
        SlideReader("odd.svs", MPP, MPP, "svs")


# -- level selection --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_mpp, level, upsampled",
    [
        (0.25, 0, False),
        (0.5, 0, False),  # no level between 0.25 and 1.0 exists, so the finer one is read and shrunk
        (0.97, 0, False),
        (0.99, 1, False),  # 1.0 µm/px is within the 2% tolerance of 0.99
        (1.0, 1, False),
        (2.0, 1, False),
        (3.9, 1, False),
        (4.0, 2, False),
        (10.0, 2, False),
        (0.2, 0, True),  # level 0 is coarser than asked for: upsample it
        (0.1, 0, True),
    ],
)
def test_the_coarsest_level_that_is_fine_enough_is_read(monkeypatch, target_mpp, level, upsampled):
    reader = open_fake(monkeypatch, FakeOpenSlide(4096, 2048, downsamples=(1, 4, 16)))
    region = read_region_at_mpp(reader, 100, 100, 64, 64, target_mpp)
    assert (region.native_level, region.upsampled) == (level, upsampled)
    assert region.native_mpp == reader.levels[level].mpp


def test_a_level_within_the_tolerance_is_not_an_upsample(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(2048, 2048, downsamples=(1, 4)), mpp=0.2529)
    region = read_region_at_mpp(reader, 0, 0, 64, 64, 0.25)
    assert (region.native_level, region.upsampled) == (0, False)
    assert read_region_at_mpp(reader, 0, 0, 64, 64, 0.25, mpp_tolerance=0.001).upsampled


def test_the_coarser_axis_decides_whether_a_level_is_fine_enough(monkeypatch):
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(2048, 2048, downsamples=(1, 4)))
    reader = SlideReader("aniso.svs", 0.25, 0.30, "svs")
    assert reader.levels[0].mpp == 0.30
    assert read_region_at_mpp(reader, 0, 0, 64, 64, 0.25).upsampled


# -- output geometry --------------------------------------------------------------------------


def test_the_output_has_the_exact_pixel_size_of_the_request(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(4096, 2048, downsamples=(1, 4, 16)))
    for target_mpp in (0.25, 0.5, 1.0, 4.0):
        region = read_region_at_mpp(reader, 130.3, 71.7, 100.0, 60.0, target_mpp)
        assert region.rgb.shape == (round(60.0 / target_mpp), round(100.0 / target_mpp), 3)
        assert region.rgb.dtype == np.uint8
        assert region.origin_um == (130.3, 71.7) and region.size_um == (100.0, 60.0)
        assert region.target_mpp == target_mpp


def test_a_region_smaller_than_a_pixel_is_refused(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(512, 512))
    with pytest.raises(ValueError, match="smaller than one pixel"):
        read_region_at_mpp(reader, 0, 0, 0.1, 50, MPP)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(w_um=0), "w_um"),
        (dict(h_um=float("nan")), "h_um"),
        (dict(x_um=float("inf")), "x_um"),
        (dict(target_mpp=0), "target_mpp"),
        (dict(mpp_tolerance=-0.1), "mpp_tolerance"),
        (dict(color="sepia"), "color"),
    ],
)
def test_bad_arguments_raise(monkeypatch, kwargs, message):
    reader = open_fake(monkeypatch, FakeOpenSlide(512, 512))
    args = dict(x_um=0, y_um=0, w_um=32, h_um=32, target_mpp=MPP)
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        read_region_at_mpp(reader, **args)


def test_a_region_entirely_outside_the_slide_raises(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(400, 400))  # 100 x 100 µm
    for x, y in ((100, 0), (0, 100), (-50, 10), (10, -50), (500, 500)):
        with pytest.raises(RegionOutOfBoundsError):
            read_region_at_mpp(reader, x, y, 50, 50, MPP)


def test_an_overhanging_region_is_padded_with_white_not_shifted(monkeypatch):
    slide = FakeOpenSlide(400, 400)
    reader = open_fake(monkeypatch, slide)  # 100 x 100 µm
    inside = read_region_at_mpp(reader, 50, 50, 50, 50, MPP)  # the bottom-right quadrant, in bounds
    over = read_region_at_mpp(reader, 50, 50, 100, 100, MPP)  # the same corner, 50 µm past the edge
    assert over.rgb.shape == (400, 400, 3)
    assert np.array_equal(over.rgb[:200, :200], inside.rgb)
    assert (over.rgb[200:] == 255).all() and (over.rgb[:, 200:] == 255).all()


def test_transparent_pixels_are_composited_on_white(monkeypatch):
    slide = FakeOpenSlide(400, 400)
    read = slide.read_region

    def half_transparent(location, level, size):
        image = read(location, level, size)
        alpha = np.array(image)
        alpha[..., 3] = 0
        return Image.fromarray(alpha, mode="RGBA")

    slide.read_region = half_transparent
    reader = open_fake(monkeypatch, slide)
    assert (read_region_at_mpp(reader, 0, 0, 40, 40, MPP).rgb == 255).all()


def test_the_read_is_the_minimal_box_at_the_chosen_level(monkeypatch):
    slide = FakeOpenSlide(8192, 8192, downsamples=(1, 4, 16))
    reader = open_fake(monkeypatch, slide)
    read_region_at_mpp(reader, 400.0, 200.0, 256.0, 128.0, 1.0)  # 256 x 128 px at 1.0 µm/px
    (location, size), = slide.regions_read
    assert slide.levels_read == [1]
    assert location == (1600, 800)  # level-0 pixels of the request's top-left
    assert size == (256, 128)  # level-1 pixels: exactly the box, nothing near level-0 sized


def test_a_fractional_origin_keeps_its_sub_pixel_position(monkeypatch):
    """The read starts on a whole pixel at or before the request; resampling honours the remainder."""
    slide = FakeOpenSlide(2048, 2048, downsamples=(1, 4))
    reader = open_fake(monkeypatch, slide)
    read_region_at_mpp(reader, 10.13, 20.37, 30.0, 30.0, 1.0)
    (location, size), = slide.regions_read
    assert location == (int(np.floor(10.13 / MPP)), int(np.floor(20.37 / MPP)))
    assert size[0] >= 30 and size[1] >= 30


def test_a_read_that_no_level_can_serve_within_memory_is_refused(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(4096, 4096))  # a single level
    assert MAX_READ_PIXELS > 4096 * 4096
    monkeypatch.setattr("pipeline.slide_io.MAX_READ_PIXELS", 1000 * 1000)
    with pytest.raises(SlideReadError, match="no coarser level"):
        read_region_at_mpp(reader, 0, 0, 1024, 1024, 8.0)  # only 128 px out, but 4096 px must be read
    with pytest.raises(SlideReadError, match="exceeds"):
        read_region_at_mpp(reader, 0, 0, 1024, 1024, 0.5 / 8)


def test_an_openslide_error_becomes_a_slide_read_error(monkeypatch):
    slide = FakeOpenSlide(512, 512)
    reader = open_fake(monkeypatch, slide)

    def broken(location, level, size):
        raise openslide.OpenSlideError("TIFFRGBAImageGet failed")

    slide.read_region = broken
    with pytest.raises(SlideReadError, match="TIFFRGBAImageGet failed"):
        read_region_at_mpp(reader, 0, 0, 32, 32, MPP)


def test_helpers_keep_a_region_inside_the_slide(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(400, 400))  # 100 x 100 µm
    assert clamp_origin_um(reader, 90, -5, 20, 20) == (80.0, 0.0)
    assert centered_origin_um(reader, 5, 95, 20, 20) == (0.0, 80.0)
    assert centered_origin_um(reader, 50, 50, 20, 20) == (40.0, 40.0)
    assert centered_origin_um(reader, 50, 50, 300, 300) == (0.0, 0.0)  # larger than the slide: origin 0


# -- resampling ---------------------------------------------------------------------------------


@pytest.fixture
def resample_filters(monkeypatch):
    used = []
    real = Image.Image.resize

    def spy(self, size, resample=None, box=None, **kwargs):
        used.append(resample)
        return real(self, size, resample, box, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", spy)
    return used


def test_shrinking_uses_lanczos_and_enlarging_uses_bicubic(monkeypatch, resample_filters):
    reader = open_fake(monkeypatch, FakeOpenSlide(4096, 2048, downsamples=(1, 4, 16)))
    read_region_at_mpp(reader, 0, 0, 200, 200, 0.5)  # level 0 is 0.25: shrink
    read_region_at_mpp(reader, 0, 0, 200, 200, 0.2)  # level 0 is 0.25: enlarge
    assert resample_filters == [Image.Resampling.LANCZOS, Image.Resampling.BICUBIC]


def test_an_aligned_native_read_is_returned_pixel_for_pixel(monkeypatch):
    slide = FakeOpenSlide(512, 512)
    reader = open_fake(monkeypatch, slide)
    region = read_region_at_mpp(reader, 25.0, 12.5, 32.0, 16.0, MPP)  # whole level-0 pixels at 0.25 µm/px
    direct = np.array(slide.read_region((100, 50), 0, (128, 64)).convert("RGB"))
    assert np.array_equal(region.rgb, direct)


# -- colour -------------------------------------------------------------------------------------


def test_the_icc_profile_is_applied_and_reported(monkeypatch):
    plain = FakeOpenSlide(512, 512)
    profiled = FakeOpenSlide(512, 512, color_profile=ImageCms.createProfile("sRGB"))
    assert read_region_at_mpp(open_fake(monkeypatch, plain), 0, 0, 32, 32, MPP).icc_applied is False
    region = read_region_at_mpp(open_fake(monkeypatch, profiled), 0, 0, 32, 32, MPP)
    assert region.icc_applied is True
    assert region.rgb.shape == (128, 128, 3)


def test_an_unusable_icc_profile_fails_the_slide(monkeypatch):
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(512, 512, color_profile="no such profile.icc"))
    with pytest.raises(IccProfileError, match="cannot be converted to sRGB"):
        SlideReader("odd.svs", MPP, MPP, "svs")


class ScaleStain:
    """A stand-in stain transform: halves every channel."""

    def __init__(self):
        self.profile_id = uuid.uuid4()

    def apply(self, rgb):
        return (rgb // 2).astype(np.uint8)


def test_a_normalized_read_applies_the_stain_transform_and_names_it(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(512, 512))
    stain = ScaleStain()
    raw = read_region_at_mpp(reader, 10, 10, 40, 40, MPP)
    normalized = read_region_at_mpp(reader, 10, 10, 40, 40, MPP, color="normalized", stain=stain)
    assert (raw.color, raw.stain_profile_id) == ("raw", None)
    assert (normalized.color, normalized.stain_profile_id) == ("normalized", stain.profile_id)
    assert np.array_equal(normalized.rgb, raw.rgb // 2)


def test_colour_and_stain_arguments_must_agree(monkeypatch):
    reader = open_fake(monkeypatch, FakeOpenSlide(512, 512))
    with pytest.raises(ValueError, match="persisted stain transform"):
        read_region_at_mpp(reader, 0, 0, 32, 32, MPP, color="normalized")
    with pytest.raises(ValueError, match="color='raw'"):
        read_region_at_mpp(reader, 0, 0, 32, 32, MPP, stain=ScaleStain())


# -- real slides: the same tissue at every resolution (AC6) ------------------------------------------

SQUARE_UM = (100.0, 60.0, 40.0)  # x, y, side of a dark landmark
EXTENT_PX = (2048, 1024)  # at 0.25 µm/px: 512 x 256 µm


@pytest.fixture(scope="module")
def slides(tmp_path_factory):
    folder = tmp_path_factory.mktemp("slides")
    base_40x = tissue_rgb(*EXTENT_PX, MPP, [SQUARE_UM])
    base_20x = halve(base_40x)
    return {
        "40x": write_pyramid_tiff(folder / "scan_40x.tif", base_40x, MPP, levels=4),
        "20x": write_pyramid_tiff(folder / "scan_20x.tif", base_20x, 2 * MPP, levels=3),
    }


def landmark_box_px(rgb: np.ndarray, origin_um: tuple[float, float], mpp: float):
    """The dark square's bounding box, in the region's pixels, as (x0, y0, x1, y1) floats."""
    dark = rgb[..., 0] < 120
    ys, xs = np.nonzero(dark)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def expected_landmark_px(origin_um, mpp):
    x, y, side = SQUARE_UM
    return ((x - origin_um[0]) / mpp, (y - origin_um[1]) / mpp, (x + side - origin_um[0]) / mpp, (y + side - origin_um[1]) / mpp)


def test_real_slides_open_with_their_pyramids(slides):
    with SlideReader(str(slides["40x"]), MPP, MPP, "tiff") as reader:
        assert [round(lv.mpp, 6) for lv in reader.levels] == [0.25, 0.5, 1.0, 2.0]
        assert reader.dimensions == EXTENT_PX
        assert reader.extent_um() == (512.0, 256.0)


@pytest.mark.parametrize("target_mpp, level", [(0.25, 0), (0.5, 1), (1.0, 2), (2.0, 3)])
def test_a_landmark_lands_at_its_physical_position_at_every_resolution(slides, target_mpp, level):
    origin = (83.3, 41.9)
    with SlideReader(str(slides["40x"]), MPP, MPP, "tiff") as reader:
        region = read_region_at_mpp(reader, *origin, 100.0, 80.0, target_mpp)
    assert region.native_level == level and not region.upsampled
    assert region.rgb.shape == (round(80.0 / target_mpp), round(100.0 / target_mpp), 3)
    found = np.array(landmark_box_px(region.rgb, origin, target_mpp), dtype=float)
    assert np.abs(found - np.array(expected_landmark_px(origin, target_mpp))).max() <= 1.0


def test_a_40x_scan_and_its_20x_derivative_agree_on_shape_and_origin(slides):
    """AC6: identical shape and origin_um at 0.25 µm/px; only the 20x read is an upsample."""
    request = (83.3, 41.9, 100.0, 80.0)
    with SlideReader(str(slides["40x"]), MPP, MPP, "tiff") as scan_40x, SlideReader(
        str(slides["20x"]), 2 * MPP, 2 * MPP, "tiff"
    ) as scan_20x:
        native = read_region_at_mpp(scan_40x, *request, MPP)
        derived = read_region_at_mpp(scan_20x, *request, MPP)
    assert native.rgb.shape == derived.rgb.shape == (320, 400, 3)
    assert native.origin_um == derived.origin_um == (83.3, 41.9)
    assert (native.upsampled, derived.upsampled) == (False, True)
    assert (native.native_mpp, derived.native_mpp) == (0.25, 0.5)
    box_native = np.array(landmark_box_px(native.rgb, native.origin_um, MPP), dtype=float)
    box_derived = np.array(landmark_box_px(derived.rgb, derived.origin_um, MPP), dtype=float)
    assert np.abs(box_native - box_derived).max() <= 2.0  # the derivative is blurred by up to a 20x pixel


def test_real_slides_pad_the_edge_like_the_fake(slides):
    with SlideReader(str(slides["40x"]), MPP, MPP, "tiff") as reader:
        region = read_region_at_mpp(reader, 480.0, 230.0, 64.0, 64.0, MPP)
    assert region.rgb.shape == (256, 256, 3)
    assert (region.rgb[-1, -1] == 255).all()  # 32 µm past the right edge and 38 µm below the bottom


# -- threads (AC7) -------------------------------------------------------------------------------------

THREADS = 16
READS_PER_THREAD = 500


def random_requests(count: int, seed: int) -> list[tuple[float, float, float, float, float]]:
    rng = np.random.default_rng(seed)
    extent_w, extent_h = EXTENT_PX[0] * MPP, EXTENT_PX[1] * MPP
    requests = []
    for _ in range(count):
        target = float(rng.choice([0.25, 0.5, 1.0, 2.0, 0.7]))
        w, h = (float(v) for v in rng.uniform(16, 48, size=2))
        requests.append((float(rng.uniform(0, extent_w - w)), float(rng.uniform(0, extent_h - h)), w, h, target))
    return requests


def region_digest(reader: SlideReader, request) -> str:
    x, y, w, h, target = request
    region = read_region_at_mpp(reader, x, y, w, h, target)
    return hashlib.sha256(region.rgb.tobytes() + str(region.rgb.shape).encode()).hexdigest()


def test_concurrent_reads_are_byte_identical_to_single_threaded_reads(slides):
    """AC7: 16 threads x 500 reads on thread-local handles match the single-threaded result."""
    requests = random_requests(READS_PER_THREAD, seed=7)
    with SlideReader(str(slides["40x"]), MPP, MPP, "tiff") as reader:
        expected = [region_digest(reader, request) for request in requests]

        results: dict[int, list[str]] = {}
        errors: list[BaseException] = []
        start = threading.Barrier(THREADS)

        def worker(n: int) -> None:
            try:
                order = np.random.default_rng(n).permutation(len(requests))
                start.wait()
                results[n] = [region_digest(reader, requests[i]) for i in order]
                results[n] = [results[n][int(np.where(order == i)[0][0])] for i in range(len(requests))]
            except BaseException as exc:  # noqa: BLE001 - re-raised below, from the main thread
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors[0]
        assert all(results[n] == expected for n in range(THREADS))
        assert len(reader._handles) == THREADS + 1  # every thread read through its own handle


def test_every_thread_handle_is_closed_with_the_reader(monkeypatch):
    handles = []

    def opener(path):
        handles.append(FakeOpenSlide(256, 256))
        return handles[-1]

    monkeypatch.setattr(openslide, "OpenSlide", opener)
    reader = SlideReader("fake.svs", MPP, MPP, "svs")
    threads = [threading.Thread(target=read_region_at_mpp, args=(reader, 0, 0, 16, 16, MPP)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(handles) == 5
    reader.close()
    assert all(handle.closed for handle in handles)
