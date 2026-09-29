"""Patches reconstructed from the GCS DeepZoom pyramid tiles that Stage 2 and ingest wrote.

Reads of the slide file itself go through ``pipeline/slide_io.py``.
"""
import io

from PIL import Image


def extract_patch_from_pyramid(
    slide_id: str,
    cx_um: float,
    cy_um: float,
    field_um: float,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    width_px: int = 20000,
    height_px: int = 20000,
    layer: str = "norm"
) -> Image.Image | None:
    """
    Rapidly reconstructs a high-resolution microscopic field directly from GCS DeepZoom pyramid tiles.
    Avoids downloading massive raw WSI files over the network.
    """
    import math
    from app.core.config import settings
    from app.core.gcs import download_blob_as_bytes

    try:
        max_dim = max(width_px, height_px)
        max_level = int(math.ceil(math.log2(max_dim))) if max_dim > 0 else 18
        
        out_size = 512
        crop_mpp = field_um / float(out_size)
        
        scale_ratio = (mpp_x / crop_mpp)
        level_offset = int(round(math.log2(scale_ratio))) if scale_ratio > 0 else 0
        z = max(0, min(max_level, max_level + level_offset))
        
        level_scale = 2.0 ** (z - max_level)
        
        cx_z = (cx_um / mpp_x) * level_scale
        cy_z = (cy_um / mpp_y) * level_scale
        w_z = (field_um / mpp_x) * level_scale
        h_z = (field_um / mpp_y) * level_scale
        
        x0_z = cx_z - w_z / 2.0
        y0_z = cy_z - h_z / 2.0
        x1_z = x0_z + w_z
        y1_z = y0_z + h_z
        
        c_min = max(0, int(math.floor(x0_z / 256.0)))
        c_max = int(math.floor(x1_z / 256.0))
        r_min = max(0, int(math.floor(y0_z / 256.0)))
        r_max = int(math.floor(y1_z / 256.0))
        
        total_tiles = (c_max - c_min + 1) * (r_max - r_min + 1)
        if total_tiles > 16 or total_tiles <= 0:
            return None
            
        stitch_w = (c_max - c_min + 1) * 256
        stitch_h = (r_max - r_min + 1) * 256
        if stitch_w <= 0 or stitch_h <= 0 or stitch_w > 4096 or stitch_h > 4096:
            return None
            
        mosaic = Image.new("RGB", (stitch_w, stitch_h), (240, 230, 235))
        tiles_fetched = 0
        
        for c in range(c_min, c_max + 1):
            for r in range(r_min, r_max + 1):
                tile_bytes = None
                for ext in (".png", ".jpg"):
                    tile_blob = f"{slide_id}/{layer}/{z}/{c}_{r}{ext}"
                    try:
                        tile_bytes = download_blob_as_bytes(settings.GCS_PYRAMIDS_BUCKET, tile_blob)
                        break
                    except Exception:
                        pass
                if tile_bytes:
                    try:
                        t_img = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
                        paste_x = (c - c_min) * 256
                        paste_y = (r - r_min) * 256
                        mosaic.paste(t_img, (paste_x, paste_y))
                        tiles_fetched += 1
                    except Exception:
                        pass
                    
        # Issue #730: Require >= 75% coverage (or 100% when total <= 4)
        # Avoids returning misleading pink/incomplete mosaics when pyramid tiles are missing
        min_required_tiles = total_tiles if total_tiles <= 4 else int(math.ceil(total_tiles * 0.75))
        if tiles_fetched < min_required_tiles:
            return None
            
        crop_x0 = int(round(x0_z - c_min * 256.0))
        crop_y0 = int(round(y0_z - r_min * 256.0))
        crop_x1 = int(round(x1_z - c_min * 256.0))
        crop_y1 = int(round(y1_z - r_min * 256.0))
        
        cropped = mosaic.crop((crop_x0, crop_y0, crop_x1, crop_y1))
        return cropped.resize((out_size, out_size), Image.Resampling.BILINEAR)
    except Exception as e:
        print(f"[Tile Patch Stitching Error] {e}")
        return None
