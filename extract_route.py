"""Crop + chromatic HSV / neutral brightness segmentation; NOT semantic/GPS recognition."""
import argparse
from collections import deque
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from route_components import select_components
from mask_cleanup import fill_supported_interiors, complete_open_interiors

MAX_PIXELS = 12_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def parse_color(value):
    if len(value) != 7 or not value.startswith('#'):
        raise ValueError('Color must be #RRGGBB')
    try:
        return tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
    except ValueError:
        raise ValueError('Color must be #RRGGBB') from None


def filter_components(mask, minimum, minimum_span=1):
    """八邻域连通分量筛选：只按面积和跨度过滤，不按最大分量猜路线。"""
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
            if max(max(xx)-min(xx)+1, max(yy)-min(yy)+1) < minimum_span:
                continue
            kept[yy, xx] = True
            components.append({'pixels': len(points), 'bbox_crop_xyxy':
                               [min(xx), min(yy), max(xx)+1, max(yy)+1]})
    components.sort(key=lambda c: c['pixels'], reverse=True)
    return kept, components


def segment(image, color, hue_tolerance=20, min_saturation=0.35, min_value=0.2, min_component=20,
            gray_value_tolerance=0.12, gray_max_saturation=0.32, gray_min_span=16):
    if not math.isfinite(hue_tolerance) or not 0 < hue_tolerance <= 180:
        raise ValueError('Hue tolerance must be in (0, 180] degrees')
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (min_saturation, min_value)):
        raise ValueError('Saturation/value thresholds must be in [0, 1]')
    if isinstance(min_component, bool) or not isinstance(min_component, int) or min_component < 1:
        raise ValueError('min_component must be a positive integer')
    if any(not math.isfinite(v) or not 0 <= v <= 1
           for v in (gray_value_tolerance, gray_max_saturation)):
        raise ValueError('Gray thresholds must be finite and in [0, 1]')
    if type(gray_min_span) is not int or gray_min_span < 1:
        raise ValueError('gray_min_span must be a positive integer')
    if image.width*image.height > MAX_PIXELS:
        raise ValueError('Crop exceeds pixel limit')
    target = np.array(Image.new('RGB', (1, 1), color).convert('HSV'))[0, 0]
    hsv = np.array(image.convert('RGB').convert('HSV')).astype(np.float32)
    neutral = int(target[1]) < 64  # 低饱和度颜色的色相不稳定，改用明度和饱和度范围
    if neutral:
        # 明确匹配中性明度；不能简单放宽色相，把整张底图都选进来。
        raw = ((hsv[:, :, 1] <= gray_max_saturation*255)
               & (np.abs(hsv[:, :, 2]-float(target[2])) <= gray_value_tolerance*255))
    else:
        dh = np.abs(hsv[:, :, 0]-float(target[0])) * (360/255)
        dh = np.minimum(dh, 360-dh)
        raw = ((dh <= hue_tolerance) & (hsv[:, :, 1] >= min_saturation*255)
               & (hsv[:, :, 2] >= min_value*255))
    # 灰色路线优先保留实心带：先补同一分量内有原图支持的短截线，再补封闭孔洞。
    raw_count = int(raw.sum())
    cleanup = {'filled_holes': 0, 'added_pixels': 0, 'method': 'not_applied'}
    if neutral:
        raw, open_stats = complete_open_interiors(raw, image)
        raw, cleanup = fill_supported_interiors(raw, image)
        cleanup.update(open_stats)
        cleanup['added_pixels'] += open_stats['open_added_pixels']
    mask, components = filter_components(raw, min_component, gray_min_span if neutral else 1)
    return mask, {'raw_pixels': raw_count, 'selected_pixels': int(mask.sum()),
                  'segmentation_mode': 'neutral_brightness' if neutral else 'chromatic_hsv',
                  'mask_cleanup': cleanup, 'components': components}


def extract(image_path, output_dir, color, crop=None, hue_tolerance=20,
            min_saturation=0.35, min_value=0.2, min_component=20,
            gray_value_tolerance=0.12, gray_max_saturation=0.32, gray_min_span=16,
            component_ids=None):
    source = Path(image_path)
    if source.stat().st_size > 50_000_000:
        raise ValueError('Image exceeds 50 MB limit')
    out = Path(output_dir)
    reserved = [out/n for n in ('mask.png', 'overlay.png', 'extraction.json', 'candidates-mask.png', 'components-overlay.png', 'source-crop.png', 'component-ids.png')]
    if source.resolve() in [p.resolve() for p in reserved]:
        raise ValueError('Output must not overwrite input image')
    with Image.open(source) as opened:
        if opened.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError(f'Unsupported decoded image format: {opened.format or "unknown"}; only PNG/JPEG/WebP supported. Filename extension is not format evidence; convert locally to PNG before retrying.')
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
    mask, stats = segment(region, parse_color(color), hue_tolerance, min_saturation, min_value, min_component,
                          gray_value_tolerance, gray_max_saturation, gray_min_span)
    # 颜色匹配结果完整保存；目标分量选择与颜色/最小面积筛选分离。
    candidates_mask = mask.copy()
    mask, selection, component_labels = select_components(mask, component_ids, return_labels=True)
    stats['color_candidate_pixels'] = stats['selected_pixels']
    stats['selected_pixels'] = int(mask.sum())
    stats['component_selection'] = selection
    stats['components'] = [c for c in selection['candidates'] if c['selected']]
    audit = np.array(region)
    audit[candidates_mask & ~mask] = (255, 160, 0)
    audit[mask] = (0, 190, 100)
    from PIL import ImageDraw
    audit_image = Image.fromarray(audit)
    draw = ImageDraw.Draw(audit_image)
    for c in selection['candidates']:
        x0, y0, x1, y1 = c['bbox_crop_xyxy']
        draw.rectangle((x0, y0, x1-1, y1-1), outline=(255, 0, 80))
        draw.text((x0, max(0, y0-12)), str(c['id']), fill=(0, 0, 0))
    overlay = np.array(region)
    overlay[mask] = (overlay[mask]*0.35 + np.array([255, 0, 80])*0.65).astype(np.uint8)
    warnings = ['Color segmentation only: same-colored icons/text may be included; occluded lines may be missed.',
                'No centerline, traversal order, geolocation or GPS reconstruction.',
                'Crop coordinates refer to EXIF-oriented image; bbox right/bottom are exclusive.']
    warnings.append(selection['reason'])
    if stats['segmentation_mode'] == 'neutral_brightness':
        warnings.append('Neutral brightness candidate only: same-gray text/roads may remain; short fragments are filtered. No gaps are bridged. Inspect overlay and use a map-only crop.')
    if stats['mask_cleanup']['added_pixels']:
        warnings.append('Source-supported narrow open/enclosed interiors were filled before thinning; inspect overlay. This is heuristic, not recovered traversal evidence.')
    if not stats['selected_pixels']:
        warnings.append('No pixels retained. Check crop, target color and thresholds.')
    if stats['selected_pixels'] > mask.size*0.2:
        warnings.append('Over 20% of crop selected; likely excessive background selection.')
    if len(stats['components']) > 1:
        warnings.append('Multiple components retained; do not assume they form a single ordered route.')
    report = {'status': 'needs_confirmation' if stats['selected_pixels'] else 'no_detection',
              'verified': False, 'original_gps': False, 'method': 'manual_crop_color_threshold_v2',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'oriented_image_size': [w, h], 'crop_xyxy': crop,
              'parameters': {'color': color, 'hue_tolerance_degrees': hue_tolerance,
                             'min_saturation': min_saturation, 'min_value': min_value,
                             'min_component': min_component,
                             'gray_value_tolerance': gray_value_tolerance,
                             'gray_max_saturation': gray_max_saturation,
                             'gray_min_span': gray_min_span},
              **stats, 'warnings': warnings}
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(candidates_mask.astype(np.uint8)*255).save(out/'candidates-mask.png')
    # 24 位无损编号图：0 为背景，RGB 为编号低/中/高字节。
    ids_rgb = np.stack([(component_labels >> shift) & 255 for shift in (0, 8, 16)], axis=-1).astype(np.uint8)
    Image.fromarray(ids_rgb).save(out/'component-ids.png')
    region.save(out/'source-crop.png')
    audit_image.save(out/'components-overlay.png')
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
    parser.add_argument('--gray-value-tolerance', type=float, default=0.12)
    parser.add_argument('--gray-max-saturation', type=float, default=0.32)
    parser.add_argument('--gray-min-span', type=int, default=16)
    parser.add_argument('--component-ids', nargs='+', type=int, help='显式选择当前分量编号，可多个；省略则保守自动筛选')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        report = extract(args.image, args.output_dir, args.color, args.crop, args.hue_tolerance,
                         args.min_saturation, args.min_value, args.min_component,
                         args.gray_value_tolerance, args.gray_max_saturation, args.gray_min_span, args.component_ids)
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        parser.error(str(exc))
    print(json.dumps({'status': report['status'], 'selected_pixels': report['selected_pixels'],
                      'components': len(report['components']), 'output_dir': args.output_dir}))


if __name__ == '__main__':
    main()
