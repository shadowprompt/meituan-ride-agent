"""Bounded local color proposals, not route recognition or confidence estimates."""
import io
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageOps

MAX_SOURCE_BYTES = 50_000_000
MAX_PIXELS = 12_000_000
MAX_SIDE = 512
MAX_CANDIDATES = 5
HUE_RADIUS = 12  # Pillow HSV bins; circular distance, approximately 17 degrees
HUE_SEPARATION = 32  # approximately 45 degrees
WARNING = ('Heuristic color candidates only, not semantic route identification or '
           'confidence. Neutral candidates are brightness proposals and may include text. No map crop or geolocation is inferred. Counts refer to '
           'sampled pixels; downsampling can miss thin features. Bounding boxes '
           'enclose sampled support, not an exact full-resolution segmentation.')


def _same_file(a, b):
    if a.resolve() == b.resolve():
        return True
    try:
        return a.samefile(b)
    except FileNotFoundError:
        return False


def _atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix='.colors-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect_colors(image_path, output_dir, crop=None):
    """Return JSON-safe proposals and write colors.json/color-preview.png.

    crop is None or four Python integers [left, top, right, bottom] in the
    EXIF-oriented source frame (right/bottom exclusive). At most five circular
    hue-separated saturated-color proposals are produced. supporting_pixel_count
    counts the <=512-side nearest-neighbor sample, NOT original source pixels.
    bbox_xyxy is in original EXIF-oriented coordinates, right/bottom exclusive.
    Invalid/oversized input or output/source aliases raise ValueError.
    """
    source, out = Path(image_path), Path(output_dir)
    targets = [out / 'colors.json', out / 'color-preview.png']
    if any(_same_file(source, target) for target in targets):
        raise ValueError('Output must not overwrite input image')
    if _same_file(targets[0], targets[1]):
        raise ValueError('Output files must be distinct')
    with source.open('rb') as stream:
        if os.fstat(stream.fileno()).st_size > MAX_SOURCE_BYTES:
            raise ValueError('Source exceeds byte limit')
        encoded = stream.read(MAX_SOURCE_BYTES + 1)
    if len(encoded) > MAX_SOURCE_BYTES:
        raise ValueError('Source exceeds byte limit')
    try:
        with Image.open(io.BytesIO(encoded)) as opened:
            if opened.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Only PNG/JPEG/WebP supported')
            if opened.width * opened.height > MAX_PIXELS:
                raise ValueError('Source exceeds pixel limit')
            oriented = ImageOps.exif_transpose(opened)
            w, h = oriented.size
            if crop is None:
                bounds = [0, 0, w, h]
            else:
                if (not isinstance(crop, (list, tuple)) or len(crop) != 4
                        or any(type(v) is not int for v in crop)):
                    raise ValueError('Crop must contain four integers')
                bounds = list(crop)
                x0, y0, x1, y1 = bounds
                if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
                    raise ValueError('Crop lies outside oriented image or is empty')
            region = oriented.crop(tuple(bounds))
            # Resize before allocating NumPy arrays; alpha is composited over white.
            cw, ch = region.size
            scale = min(1.0, MAX_SIDE / max(cw, ch))
            size = (max(1, int(cw * scale)), max(1, int(ch * scale)))
            region = region.resize(size, Image.Resampling.NEAREST).convert('RGBA')
            sample = Image.new('RGBA', size, 'white')
            sample.alpha_composite(region)
            sample = sample.convert('RGB')
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError('Invalid or oversized image') from exc

    rgb = np.asarray(sample)
    hsv = np.asarray(sample.convert('HSV'))
    hue = hsv[:, :, 0].astype(np.int16)
    saturated = (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 51)
    # 色相是环形量：红色横跨 0/255，计算距离时不能按普通线性数值切开。
    hist = np.bincount(hue[saturated], minlength=256)
    bins = np.arange(256)
    distances = np.abs(bins[:, None] - bins[None, :])
    distances = np.minimum(distances, 256 - distances)
    scores = (distances <= HUE_RADIUS).astype(np.int64) @ hist
    eligible = np.ones(256, dtype=bool)
    minimum = max(3, math.ceil(size[0] * size[1] * 0.0001))
    candidates, masks = [], []
    # 只按采样颜色支持度排序，不输出语义置信度；地图按钮和绿地也可能入选。
    for _ in range(MAX_CANDIDATES):
        ranked = np.where(eligible, scores, -1)
        center = int(np.argmax(ranked))  # ascending hue resolves all ties
        if int(ranked[center]) < minimum:
            break
        dh = np.abs(hue - center)
        mask = saturated & (np.minimum(dh, 256 - dh) <= HUE_RADIUS)
        yy, xx = np.nonzero(mask)
        color = np.median(rgb[mask], axis=0).astype(np.uint8)
        x0, y0, _, _ = bounds
        bbox = [x0 + int(xx.min()) * cw // size[0],
                y0 + int(yy.min()) * ch // size[1],
                x0 + math.ceil((int(xx.max()) + 1) * cw / size[0]),
                y0 + math.ceil((int(yy.max()) + 1) * ch / size[1])]
        candidates.append({'color': '#%02X%02X%02X' % tuple(color.tolist()),
                           'supporting_pixel_count': int(mask.sum()),
                           'bbox_xyxy': bbox, 'kind': 'heuristic_color_candidate',
                           'warning': 'Not semantic identification or confidence'})
        masks.append(mask)
        eligible &= distances[center] >= HUE_SEPARATION

    # Neutral/tinted-gray proposals use brightness, never undefined gray hue.
    # Keep separate from saturated proposals so consumers can inspect both honestly.
    neutral_candidates, neutral_masks = [], []
    neutral = (hsv[:, :, 1] <= 81) & (hsv[:, :, 2] >= 25) & (hsv[:, :, 2] <= 215)
    value = hsv[:, :, 2].astype(np.int16)
    brightness_hist = np.bincount(value[neutral], minlength=256)
    available = np.ones(256, dtype=bool)
    for _ in range(2):
        scores_gray = np.convolve(brightness_hist, np.ones(25, dtype=np.int64), mode='same')
        center = int(np.argmax(np.where(available, scores_gray, -1)))
        mask = neutral & (np.abs(value-center) <= 12)
        count = int(mask.sum())
        available &= np.abs(np.arange(256)-center) > 30
        if count < minimum:
            break
        if count > size[0]*size[1]*0.2:
            continue  # flat gray background is not a useful route proposal
        yy, xx = np.nonzero(mask)
        color = np.median(rgb[mask], axis=0).astype(np.uint8)
        x0, y0, _, _ = bounds
        neutral_candidates.append({
            'color': '#%02X%02X%02X' % tuple(color.tolist()),
            'supporting_pixel_count': count,
            'bbox_xyxy': [x0+int(xx.min())*cw//size[0], y0+int(yy.min())*ch//size[1],
                          x0+math.ceil((int(xx.max())+1)*cw/size[0]),
                          y0+math.ceil((int(yy.max())+1)*ch/size[1])],
            'kind': 'heuristic_neutral_candidate',
            'warning': 'Brightness proposal only; may include text, not route confidence'})
        neutral_masks.append(mask)

    report = {'status': 'needs_confirmation' if candidates or neutral_candidates else 'no_signal',
              'warning': WARNING, 'crop': bounds, 'image_size': [w, h],
              'coordinate_frame': 'original_exif_oriented_pixels',
              'bbox_convention': 'xyxy_right_bottom_exclusive',
              'sample_size': list(size), 'count_frame': 'sampled_pixels',
              'sampling': 'nearest_neighbor', 'minimum_support': minimum,
              'candidates': candidates, 'neutral_candidates': neutral_candidates}
    candidates = candidates + neutral_candidates
    masks = masks + neutral_masks
    # Fixed-size-bounded contact sheet: sampled original plus one panel/candidate.
    panel_w, panel_h = max(size[0], 300), size[1] + 60
    sheet = Image.new('RGB', (panel_w * (1 + len(candidates)), panel_h), 'white')
    sheet.paste(sample, (0, 60))
    draw = ImageDraw.Draw(sheet)
    draw.text((5, 5), 'Sampled crop - heuristic only', fill='black')
    if not candidates:
        draw.text((5, 25), 'No useful color signal', fill='black')
    for i, (candidate, mask) in enumerate(zip(candidates, masks), 1):
        left = i * panel_w
        draw.rectangle((left + 5, 5, left + 35, 30), fill=candidate['color'])
        draw.text((left + 42, 5), candidate['color'], fill='black')
        draw.text((left + 42, 22), str(candidate['supporting_pixel_count']) + ' sample pixels', fill='black')
        draw.text((left + 5, 42), 'Highlighted support; NOT confidence', fill='black')
        highlight = (rgb.astype(np.uint16) // 4).astype(np.uint8)
        highlight[mask] = rgb[mask]
        sheet.paste(Image.fromarray(highlight), (left, 60))
    preview = io.BytesIO()
    sheet.save(preview, format='PNG')
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(targets[1], preview.getvalue())
    _atomic_write(targets[0], (json.dumps(report, indent=2) + '\n').encode('utf-8'))
    return report
