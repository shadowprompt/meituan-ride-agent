"""有原图证据约束的掩码内部填充；不跨越断点、不连接不同连通分量。"""
from collections import deque
import numpy as np


def fill_supported_interiors(mask, image, max_area=12000, max_width=24):
    """只检查完全封闭的孔洞，保留开放缺口、地图背景和宽大的真实环形区域。

    灰色实线被错误选成两侧边缘时，其内部仍应有低饱和度、相近明度
    的原图像素。白色箭头只允许作为很小的内部孔洞填充，不做形态学闭运算。
    阈值是保守启发式，不构成语义路线识别；调用方必须公开新增像素和告警。
    """
    source = np.asarray(mask, dtype=bool)
    result = source.copy()
    h, w = source.shape
    hsv = np.asarray(image.convert('HSV')).astype(np.float32)
    seen = source.copy()
    added = holes = 0
    for y, x in zip(*np.nonzero(~source)):
        if seen[y, x]:
            continue
        queue = deque([(int(y), int(x))]); seen[y, x] = True
        points, boundary = [], set()
        touches_border = False
        while queue:
            cy, cx = queue.popleft(); points.append((cy, cx))
            touches_border |= cy in (0, h-1) or cx in (0, w-1)
            # 背景使用四邻域：避免通过对角接触泄漏，但不会把开放端点封起来。
            for ny, nx in ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1)):
                if not (0 <= ny < h and 0 <= nx < w):
                    continue
                if source[ny,nx]:
                    boundary.add((ny,nx))
                elif not seen[ny,nx]:
                    seen[ny,nx] = True; queue.append((ny,nx))
        if touches_border or len(points) > max_area or not boundary:
            continue
        yy, xx = zip(*points)
        by, bx = zip(*boundary)
        # 从孔洞边缘向内算距离，仅接受厚度有界的窄带；不填宽大的真实环。
        inside = set(points)
        frontier = deque()
        distance = {}
        for p in points:
            cy,cx = p
            if any((ny,nx) not in inside for ny,nx in
                   ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1))):
                distance[p] = 1; frontier.append(p)
        while frontier:
            cy,cx = frontier.popleft()
            for q in ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1)):
                if q in inside and q not in distance:
                    distance[q] = distance[(cy,cx)]+1; frontier.append(q)
        if max(distance.values())*2 > max_width:
            continue
        values = hsv[yy,xx,2]
        saturation = hsv[yy,xx,1]
        reference = float(np.median(hsv[by,bx,2]))
        supported = ((saturation <= 0.32*255) & (values <= 0.85*255)
                     & (np.abs(values-reference) <= 0.22*255))
        tiny_arrow = len(points) <= 64 and max(max(xx)-min(xx),max(yy)-min(yy)) <= 16
        # 长孔洞必须绝大部分有灰色实线证据；小箭头也必须是中性像素。
        if supported.mean() < .85 and not (tiny_arrow and (saturation <= .32*255).all()):
            continue
        result[yy,xx] = True
        added += len(points); holes += 1
    return result, {'filled_holes': holes, 'added_pixels': added,
                    'max_area': max_area, 'max_width': max_width,
                    'method': 'enclosed_source_supported_interiors_v1'}
