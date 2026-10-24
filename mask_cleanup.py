"""有原图证据约束的掩码内部填充；开放补全不连接不同原始连通分量。"""
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
        tiny_arrow = len(points) <= 160 and max(max(xx)-min(xx),max(yy)-min(yy)) <= 20
        # 长孔洞必须绝大部分有灰色实线证据；小箭头也必须是中性像素。
        tiny_dark = len(points) <= 16 and (saturation <= .5*255).all() and (values <= .85*255).all()
        if supported.mean() < .85 and not tiny_dark and not (tiny_arrow and (saturation <= .32*255).all()):
            continue
        result[yy,xx] = True
        added += len(points); holes += 1
    return result, {'filled_holes': holes, 'added_pixels': added,
                    'max_area': max_area, 'max_width': max_width,
                    'method': 'enclosed_source_supported_interiors_v1'}


def complete_open_interiors(mask, image, max_width=24):
    """在同一原始分量的相对边缘之间补窄带，不做无条件闭运算。

    每条短截线内部必须全部具有低饱和、相近明度的原图支持；白色背景、
    道路间隔和箭头均不能作为开放补线证据。不同原始分量绝不连接。
    四方向截线覆盖斜线；上限按欧氏距离计算而不是框宽。不是语义识别。
    """
    source = np.asarray(mask, dtype=bool)
    h, w = source.shape
    labels = np.zeros((h,w), dtype=np.int32)
    label = 0
    for y,x in zip(*np.nonzero(source)):
        if labels[y,x]:
            continue
        label += 1; labels[y,x] = label
        queue = deque([(int(y),int(x))])
        while queue:
            cy,cx = queue.popleft()
            for ny in range(max(0,cy-1),min(h,cy+2)):
                for nx in range(max(0,cx-1),min(w,cx+2)):
                    if source[ny,nx] and not labels[ny,nx]:
                        labels[ny,nx] = label; queue.append((ny,nx))
    hsv = np.asarray(image.convert('RGB').convert('HSV')).astype(np.float32)
    result = source.copy()
    accepted = 0
    for dy,dx in ((0,1),(1,0),(1,1),(1,-1)):
        for length in range(2, int(max_width/(2**.5 if dx and dy else 1))+1):
            # 只在两端都位于图内的截线上计算；不使用滚动数组，防止边界回绕。
            y0,y1 = 0,h-dy*length
            x0,x1 = max(0,-dx*length),min(w,w-dx*length)
            if y1<=y0 or x1<=x0:
                continue
            a = labels[y0:y1,x0:x1]
            b = labels[y0+dy*length:y1+dy*length,x0+dx*length:x1+dx*length]
            valid = (a>0)&(a==b)
            ref = (hsv[y0:y1,x0:x1,2]+hsv[y0+dy*length:y1+dy*length,x0+dx*length:x1+dx*length,2])/2
            for k in range(1,length):
                v = hsv[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k]
                # 开放补全比封闭孔洞更严格；不容许亮背景或无证据的白箭头。
                valid &= ((v[:,:,1]<=.32*255)&(v[:,:,2]<=.85*255)
                          &(np.abs(v[:,:,2]-ref)<=.24*255))
            if not valid.any():
                continue
            accepted += int(valid.sum())
            for k in range(1,length):
                result[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k] |= valid
    return result, {'open_added_pixels':int((result & ~source).sum()),
                    'supported_cross_sections':accepted, 'open_max_width':max_width,
                    'open_method':'same_component_source_supported_cross_sections_v1'}
