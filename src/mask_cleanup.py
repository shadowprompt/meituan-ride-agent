"""有原图证据约束的掩码内部填充；开放补全须有局部原图证据。"""
from collections import deque
import numpy as np


def thin_edge_evidence(mask, maximum=3):
    """用最窄穿线宽度识别薄边，不把斜边的切向长游程误判为实心臂。

    每个方向同时计算两侧连续原始像素，总宽度不超过三像素才是薄边。
    实心臂即便采样点位于边界，其向内游程仍计入；图外未知按实心处理。
    只使用原始掩码，新增像素不会滚动成为下一轮证据。
    """
    source = np.asarray(mask, dtype=bool)
    h, w = source.shape
    narrowest = np.full(source.shape, 2*maximum+1, dtype=np.uint8)
    for dy, dx in ((0,1),(1,0),(1,1),(1,-1)):
        width = source.astype(np.uint8)
        for sign in (-1,1):
            run = source.copy()
            for k in range(1, maximum+1):
                sy, sx = sign*dy*k, sign*dx*k
                sample = np.ones_like(source)
                y0,y1 = max(0,-sy),min(h,h-sy)
                x0,x1 = max(0,-sx),min(w,w-sx)
                sample[y0:y1,x0:x1] = source[y0+sy:y1+sy,x0+sx:x1+sx]
                run &= sample
                width += run
        narrowest = np.minimum(narrowest, width)
    return source & (narrowest <= maximum)


def fill_supported_interiors(mask, image, max_area=12000, max_width=24, evidence_mask=None):
    """只检查完全封闭的孔洞，保留开放缺口、地图背景和宽大的真实环形区域。

    灰色实线被错误选成两侧边缘时，其内部仍应有低饱和度、相近明度
    的原图像素。白色箭头只允许作为很小的内部孔洞填充，不做形态学闭运算。
    阈值是保守启发式，不构成语义路线识别；调用方必须公开新增像素和告警。
    """
    source = np.asarray(mask, dtype=bool)
    # 开放补全产生的新像素不能反过来充当厚实路线的原始证据。
    shell_source = source if evidence_mask is None else np.asarray(evidence_mask, dtype=bool)
    if shell_source.shape != source.shape:
        raise ValueError("Evidence mask shape must match source mask")
    result = source.copy()
    h, w = source.shape
    hsv = np.asarray(image.convert('HSV')).astype(np.float32)
    seen = source.copy()
    added = holes = protected = 0
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
        # 封闭本身不证明是箭头：窄真实回转环也会形成小孔洞。
        # 原图支持的长带只允许薄边壳；白箭头须有厚实包围或孔内混合灰色原图证据，
        # 不把细环边界的小白中心误认成箭头。不可辨认的结构宁缺勿补。
        thin_shell, thick_shell = 0, 0
        for cy, cx in points:
            for dy, dx in ((0,1),(0,-1),(1,0),(-1,0)):
                ny, nx = cy+dy, cx+dx
                if not (0 <= ny < h and 0 <= nx < w and shell_source[ny,nx]):
                    continue
                thickness = 0
                for k in range(1,5):
                    ny, nx = cy+dy*k, cx+dx*k
                    if not (0 <= ny < h and 0 <= nx < w and shell_source[ny,nx]):
                        break
                    thickness += 1
                thin_shell += thickness <= 3
                thick_shell += thickness >= 4
        shell_total = thin_shell + thick_shell
        supported_band = supported.mean() >= .85 and thin_shell >= .85*shell_total
        compact = len(points) <= 160 and max(max(xx)-min(xx), max(yy)-min(yy)) <= 20
        # 小标记还需灰色混合证据；无证据的纯白细环保留。小灰环仍可能歧义，须确认。
        tiny_mark = (compact and (saturation <= .32*255).mean() >= .85
                     and ((shell_total > 0 and thick_shell == shell_total and len(points) <= 40)
                          or .25 <= supported.mean() < .85))
        tiny_dark = len(points) <= 16 and (saturation <= .5*255).all() and (values <= .85*255).all()
        if not supported_band and not tiny_mark and not tiny_dark:
            protected += 1
            continue
        result[yy,xx] = True
        added += len(points); holes += 1
    return result, {'filled_holes': holes, 'added_pixels': added,
                    'protected_enclosed_regions': protected,
                    'max_area': max_area, 'max_width': max_width,
                    'method': 'enclosed_source_supported_thin_shell_v2'}


def complete_open_interiors(mask, image, max_width=24, *, source_contrast=True):
    """在相对边缘之间补有原图支持的窄带，不做无条件闭运算。

    每条短截线内部必须全部具有低饱和、相近明度的原图支持；白色背景、
    道路间隔和箭头均不能作为开放补线证据。默认不连接不同原始分量；
    仅双侧薄暗边、内部更亮且至少一侧外部更亮时允许局部例外。
    四方向截线覆盖斜线；上限按欧氏距离计算而不是框宽。不是语义识别。
    原始薄边界以外的厚实线臂间隙保守保留；细到三像素以内的去回程
    与灰带两侧仍可能不可区分，不能据此声称恢复了折返或重复顺序。
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
    thin_edges = thin_edge_evidence(source)
    result = source.copy()
    accepted = rejected = contrast_sections = cross_component_sections = 0
    risk = np.zeros_like(source)
    for dy,dx in ((0,1),(1,0),(1,1),(1,-1)):
        for length in range(2, int(max_width/(2**.5 if dx and dy else 1))+1):
            # 只在两端都位于图内的截线上计算；不使用滚动数组，防止边界回绕。
            y0,y1 = 0,h-dy*length
            x0,x1 = max(0,-dx*length),min(w,w-dx*length)
            if y1<=y0 or x1<=x0:
                continue
            a = labels[y0:y1,x0:x1]
            b = labels[y0+dy*length:y1+dy*length,x0+dx*length:x1+dx*length]
            valid = (a>0)&(b>0)
            same_component = a == b
            ref = (hsv[y0:y1,x0:x1,2]+hsv[y0+dy*length:y1+dy*length,x0+dx*length:x1+dx*length,2])/2
            for k in range(1,length):
                v = hsv[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k]
                # 开放补全比封闭孔洞更严格；不容许亮背景或无证据的白箭头。
                valid &= ~source[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k]
                valid &= ((v[:,:,1]<=.32*255)&(v[:,:,2]<=.85*255)
                          &(np.abs(v[:,:,2]-ref)<=.24*255))
            if not valid.any():
                continue
            # 法向短截线须见到两侧薄暗边、较亮淡灰内带及至少一侧亮背景。
            # 另一侧可能紧邻下一条实线臂，不能要求两侧背景都宽达四像素。
            # 只有这份双侧原图证据可覆盖斜边切向厚度误判，或跨越被箭头
            # 切断的原始边缘分量；仅仅两条线颜色相同绝不构成连接许可。
            interior = np.zeros_like(ref)
            for k in range(1,length):
                interior += hsv[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k,2]
            interior /= length-1
            contrast = (thin_edges[y0:y1,x0:x1] &
                        thin_edges[y0+dy*length:y1+dy*length,
                                   x0+dx*length:x1+dx*length])
            thick = np.zeros_like(valid)
            outside_evidence = np.zeros_like(valid)
            for endpoint, sign in ((0,-1),(length,1)):
                edge = hsv[y0+dy*endpoint:y1+dy*endpoint,
                           x0+dx*endpoint:x1+dx*endpoint,2]
                contrast &= interior >= edge+12
                run = np.ones_like(valid)
                exterior_light = np.zeros_like(valid)
                for k in range(4):
                    ys = y0+dy*(endpoint+sign*k)
                    xs = x0+dx*(endpoint+sign*k)
                    sample = np.ones_like(valid)
                    light = np.zeros_like(valid)
                    iy0,iy1 = max(0,-ys),min(y1-y0,h-ys)
                    ix0,ix1 = max(0,-xs),min(x1-x0,w-xs)
                    if iy1>iy0 and ix1>ix0:
                        sample[iy0:iy1,ix0:ix1] = source[ys+iy0:ys+iy1,xs+ix0:xs+ix1]
                        light[iy0:iy1,ix0:ix1] = (hsv[ys+iy0:ys+iy1,xs+ix0:xs+ix1,2]
                                                        >= interior[iy0:iy1,ix0:ix1]+20)
                    run &= sample
                    exterior_light |= light
                thick |= run
                outside_evidence |= exterior_light
            contrast &= outside_evidence
            if not source_contrast:
                contrast[:] = False
            valid &= same_component | contrast
            thick &= ~contrast
            blocked = valid & thick
            rejected += int(blocked.sum())
            for k in range(1,length):
                risk[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k] |= blocked
            valid &= ~thick
            contrast_sections += int((valid & contrast).sum())
            cross_component_sections += int((valid & ~same_component).sum())
            accepted += int(valid.sum())
            for k in range(1,length):
                result[y0+dy*k:y1+dy*k,x0+dx*k:x1+dx*k] |= valid
    ry, rx = np.nonzero(risk)
    return result, {'protected_cross_sections': rejected,
                    'protected_gap_pixels': int(risk.sum()),
                    'protected_gap_bbox_xyxy': ([int(rx.min()), int(ry.min()), int(rx.max()+1), int(ry.max()+1)] if len(rx) else None),
                    'open_added_pixels':int((result & ~source).sum()),
                    'supported_cross_sections':accepted,
                    'contrast_supported_cross_sections':contrast_sections,
                    'cross_component_supported_sections':cross_component_sections,
                    'open_max_width':max_width,
                    'open_method':'source_supported_normal_width_contrast_v3'}
