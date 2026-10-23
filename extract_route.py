"""Manual crop + HSV color segmentation baseline; NOT semantic/GPS recognition."""
import argparse
from collections import deque
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MAX_PIXELS = 12_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def parse_color(value):
    if len(value) != 7 or not value.startswith('#'):
        raise ValueError('Color must be #RRGGBB')
    try:
        return tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
    except ValueError:
        raise ValueError('Color must be #RRGGBB') from None


def filter_components(mask, minimum):
    """8-connected components; preserve every component meeting size threshold."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    kept = np.zeros_like(mask, dtype=bool)
    components = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        todo = deque([(int(y), int(x))])
        points = []
        while todo:
            cy, cx = todo.popleft()
            points.append((cy, cx))
            for ny in range(max(0, cy-1), min(h, cy+2)):
                for nx in range(max(0, cx-1), min(w, cx+2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        todo.append((ny, nx))
        if len(points) >= minimum:
            yy, xx = zip(*points)
            kept[yy, xx] = True
            components.append({'pixels': len(points), 'bbox_crop_xyxy':
                               [min(xx), min(yy), max(xx)+1, max(yy)+1]})
    components.sort(key=lambda c: c['pixels'], reverse=True)
    return kept, components


def segment(image, color, hue_tolerance=20, min_saturation=0.35, min_value=0.2, min_component=20):
    if not math.isfinite(hue_tolerance) or not 0 < hue_tolerance <= 180:
        raise ValueError('Hue tolerance must be in (0, 180] degrees')
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (min_saturation, min_value)):
        raise ValueError('Saturation/value thresholds must be in [0, 1]')
    if isinstance(min_component, bool) or not isinstance(min_component, int) or min_component < 1:
        raise ValueError('min_component must be a positive integer')
    if image.width*image.height > MAX_PIXELS:
        raise ValueError('Crop exceeds pixel limit')
    target = np.array(Image.new('RGB', (1, 1), color).convert('HSV'))[0, 0]
    if int(target[1]) < 20:
        raise ValueError('Target color must be chromatic; gray/white/black hue is undefined')
    hsv = np.array(image.convert('RGB').convert('HSV')).astype(np.float32)
    dh = np.abs(hsv[:, :, 0]-float(target[0])) * (360/255)
    dh = np.minimum(dh, 360-dh)
    raw = (dh <= hue_tolerance) & (hsv[:, :, 1] >= min_saturation*255) & (hsv[:, :, 2] >= min_value*255)
    mask, components = filter_components(raw, min_component)
    return mask, {'raw_pixels': int(raw.sum()), 'selected_pixels': int(mask.sum()),
                  'components': components}


def extract(image_path, output_dir, color, crop=None, hue_tolerance=20,
            min_saturation=0.35, min_value=0.2, min_component=20):
    source = Path(image_path)
    if source.stat().st_size > 50_000_000:
        raise ValueError('Image exceeds 50 MB limit')
    out = Path(output_dir)
    reserved = [out/n for n in ('mask.png', 'overlay.png', 'extraction.json')]
    if source.resolve() in [p.resolve() for p in reserved]:
        raise ValueError('Output must not overwrite input image')
    with Image.open(source) as opened:
        if opened.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('Only PNG/JPEG/WebP supported')
        if opened.width*opened.height > MAX_PIXELS:
            raise ValueError('Image exceeds 12 million pixel limit')
        image = ImageOps.exif_transpose(opened).convert('RGB')
    w, h = image.size
    crop = list(crop) if crop is not None else [0, 0, w, h]
    if len(crop) != 4 or any(type(v) is not int for v in crop):
        raise ValueError('Crop must contain four integer coordinates')
    left, top, right, bottom = crop
    if not (0 <= left < right <= w and 0 <= top < bottom <= h):
        raise ValueError('Crop outside oriented image or empty')
    region = image.crop(tuple(crop))
    mask, stats = segment(region, parse_color(color), hue_tolerance, min_saturation, min_value, min_component)
    overlay = np.array(region)
    overlay[mask] = (overlay[mask]*0.35 + np.array([255, 0, 80])*0.65).astype(np.uint8)
    warnings = ['Color segmentation only: same-colored icons/text may be included; occluded lines may be missed.',
                'No centerline, traversal order, geolocation or GPS reconstruction.',
                'Crop coordinates refer to EXIF-oriented image; bbox right/bottom are exclusive.']
    if not stats['selected_pixels']:
        warnings.append('No pixels retained. Check crop, target color and thresholds.')
    if stats['selected_pixels'] > mask.size*0.2:
        warnings.append('Over 20% of crop selected; likely excessive background selection.')
    if len(stats['components']) > 1:
        warnings.append('Multiple components retained; do not assume they form a single ordered route.')
    report = {'status': 'needs_confirmation' if stats['selected_pixels'] else 'no_detection',
              'verified': False, 'original_gps': False, 'method': 'manual_crop_hsv_threshold_v1',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'oriented_image_size': [w, h], 'crop_xyxy': crop,
              'parameters': {'color': color, 'hue_tolerance_degrees': hue_tolerance,
                             'min_saturation': min_saturation, 'min_value': min_value,
                             'min_component': min_component},
              **stats, 'warnings': warnings}
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(mask.astype(np.uint8)*255).save(out/'mask.png')
    Image.fromarray(overlay).save(out/'overlay.png')
    (out/'extraction.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--crop', nargs=4, type=int, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'))
    parser.add_argument('--color', required=True, help='Quoted #RRGGBB, e.g. "#00BFA5"')
    parser.add_argument('--hue-tolerance', type=float, default=20)
    parser.add_argument('--min-saturation', type=float, default=0.35)
    parser.add_argument('--min-value', type=float, default=0.2)
    parser.add_argument('--min-component', type=int, default=20)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        report = extract(args.image, args.output_dir, args.color, args.crop, args.hue_tolerance,
                         args.min_saturation, args.min_value, args.min_component)
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        parser.error(str(exc))
    print(json.dumps({'status': report['status'], 'selected_pixels': report['selected_pixels'],
                      'components': len(report['components']), 'output_dir': args.output_dir}))


if __name__ == '__main__':
    main()
