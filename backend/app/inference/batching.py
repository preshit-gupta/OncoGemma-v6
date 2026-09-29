"""Split image lists into requests that respect a model's registry limits."""
import math


def base64_size(n_bytes: int) -> int:
    return 4 * math.ceil(n_bytes / 3)


def plan_batches(image_sizes: list[int], max_batch: int, max_request_bytes: int) -> list[range]:
    """Consecutive index ranges, each within ``max_batch`` images and ``max_request_bytes`` of base64.

    An image too large to send on its own raises ValueError rather than being dropped.
    """
    batches: list[range] = []
    start, used = 0, 0
    for index, size in enumerate(image_sizes):
        encoded = base64_size(size)
        if encoded > max_request_bytes:
            raise ValueError(f"image {index} alone is {encoded} encoded bytes, over the {max_request_bytes} limit")
        if index > start and (index - start >= max_batch or used + encoded > max_request_bytes):
            batches.append(range(start, index))
            start, used = index, 0
        used += encoded
    if start < len(image_sizes):
        batches.append(range(start, len(image_sizes)))
    return batches
