"""本地逐孔可视复核：只按人工明确选择补封闭小孔，不猜测箭头或通行顺序。"""
import argparse
from collections import deque
import hashlib
import html
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from centerline import thin, analyze

MAX_AREA = 256
MAX_EXTENT = 32


def enclosed_holes(mask):
    """按背景四邻域稳定编号；开放间隙不成为可填候选。"""
    seen = mask.copy()
    h, w = mask.shape
    holes = []
    for y, x in zip(*np.nonzero(~mask)):
        if seen[y, x]:
            continue
        q = deque([(int(y), int(x))]); seen[y, x] = True
        points = []; border = False
        while q:
            cy, cx = q.popleft(); points.append((cy, cx))
            border |= cy in (0, h-1) or cx in (0, w-1)
            for ny, nx in ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1)):
                if 0 <= ny < h and 0 <= nx < w and not seen[ny,nx]:
                    seen[ny,nx] = True; q.append((ny,nx))
        if border:
            continue
        yy, xx = np.array(points).T
        box = [int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1)]
        holes.append({'id': len(holes)+1, 'area': len(points), 'bbox_xyxy': box,
                      'eligible': len(points) <= MAX_AREA and max(box[2]-box[0],box[3]-box[1]) <= MAX_EXTENT,
                      'points': points})
    return holes


def load_inputs(source, mask_path):
    """只读本地同尺寸原图和二值掩码；拒绝缩放以免坐标错位。"""
    for path in (source, mask_path):
        if Path(path).stat().st_size > 20_000_000:
            raise ValueError('输入不得超过20MB')
    with Image.open(source) as im:
        if im.width*im.height > 1_000_000:
            raise ValueError('请先使用不超过100万像素的原图裁剪')
        image = im.convert('RGB')
    with Image.open(mask_path) as im:
        if im.size != image.size or im.mode not in ('L','1'):
            raise ValueError('掩码须是与原图裁剪同尺寸的二值灰度图')
        a = np.array(im.convert('L'))
    if not np.isin(a,[0,255]).all():
        raise ValueError('掩码只能包含0和255')
    mask = a > 0
    digest = hashlib.sha256(str(image.size).encode()+image.tobytes()+a.tobytes()).hexdigest()
    return image, mask, digest


def review(source, mask_path, output_dir, selection=None):
    image, mask, digest = load_inputs(source, mask_path)
    out = Path(output_dir)
    # 防止把原输入或人工选择文件覆盖成结果。所有产物限定在输出目录。
    names = ('source.png','mask-before.png','mask-corrected.png','numbered.png',
             'review.html','review.json','result.json','comparison.png')
    inputs = [Path(source).resolve(), Path(mask_path).resolve()]
    if selection is not None:
        inputs.append(Path(selection).resolve())
    if any((out/n).resolve() in inputs for n in names):
        raise ValueError('输出不得覆盖输入')
    holes = enclosed_holes(mask)
    if len(holes) > 128:
        raise ValueError('孔洞超过128个，请缩小裁剪区域')
    manifest = {'version': 1, 'fingerprint': digest, 'coordinate_system': 'crop_pixels_xy_top_left',
                'size': list(image.size), 'max_area': MAX_AREA, 'max_extent': MAX_EXTENT,
                'holes': [{k:v for k,v in hole.items() if k != 'points'} for hole in holes],
                'verified': False, 'status': 'needs_confirmation'}
    corrected = mask.copy(); ids = []
    if selection is not None:
        if Path(selection).stat().st_size > 16384:
            raise ValueError('选择文件过大')
        request = json.loads(Path(selection).read_text(encoding='utf-8'))
        if not isinstance(request, dict):
            raise ValueError('选择文件须为JSON对象')
        if (request.get('fingerprint') != digest or request.get('confirm_source_reviewed') is not True):
            raise ValueError('原图或掩码已改变，或未明确确认原图复核；请重新生成预览')
        ids = request.get('fill_hole_ids')
        if not isinstance(ids,list) or len(ids)>128 or any(type(i) is not int for i in ids) or len(ids)!=len(set(ids)):
            raise ValueError('需给出不重复的整数孔洞编号')
        allowed = {h['id']:h for h in holes if h['eligible']}
        if any(i not in allowed for i in ids):
            raise ValueError('不可填开放间隙、大孔或不存在的编号')
        for i in ids:
            yy,xx = np.array(allowed[i]['points']).T
            corrected[yy,xx] = True
    out.mkdir(parents=True,exist_ok=True)
    image.save(out/'source.png')
    Image.fromarray(mask.astype('uint8')*255).save(out/'mask-before.png')
    numbered = image.copy(); draw = ImageDraw.Draw(numbered)
    for hole in holes:
        box = hole['bbox_xyxy']; draw.rectangle(box,outline='red',width=1)
        draw.text((max(0,box[0]-12),max(0,box[1]-10)),str(hole['id']),fill='red')
    numbered.save(out/'numbered.png')
    # 每个候选都给出未叠加原图、孔洞红色标记和二值掩码，避免涂层掩盖间隙。
    rows = []
    for hole in holes:
        box = hole['bbox_xyxy']; x0,y0,x1,y1 = box
        b = (max(0,x0-16),max(0,y0-16),min(mask.shape[1],x1+16),min(mask.shape[0],y1+16))
        marked = np.array(image).copy(); yy,xx = np.array(hole['points']).T; marked[yy,xx] = [255,0,80]
        tiles = [image.crop(b),Image.fromarray(marked).crop(b),Image.fromarray(mask.astype('uint8')*255).convert('RGB').crop(b)]
        sheet = Image.new('RGB',(tiles[0].width*3,tiles[0].height),'white')
        for j,tile in enumerate(tiles): sheet.paste(tile,(j*tile.width,0))
        # 每孔有限放大，全部候选总量也有界。
        name = f'hole-{hole["id"]}.png'
        if (out/name).resolve() in inputs:
            raise ValueError('输出不得覆盖输入')
        sheet.resize((sheet.width*4,sheet.height*4),Image.Resampling.NEAREST).save(out/name)
        disabled = '' if hole['eligible'] else 'disabled'
        rows.append(f'<section><label><input type="checkbox" value="{hole["id"]}" {disabled}>孔 {hole["id"]}，面积 {hole["area"]}，框 {box}</label><br><img src="{html.escape(name)}"></section>')
    page = '''<!doctype html><meta charset="utf-8"><title>逐孔人工复核</title>
<style>body{font-family:sans-serif;max-width:1100px;margin:auto}section{border:1px solid #aaa;margin:16px;padding:8px}img{max-width:100%;image-rendering:pixelated}</style>
<h1>逐孔人工复核（默认全部保留）</h1><p>仅本地文件，不发送网络请求。每行依次是原图、当前孔洞标红、掩码。封闭、颜色浅或骨架成环都不证明是白箭头。真实回转孔、相邻臂间隙及不确定的混合孔必须保留；不能修正一个孔的一部分，请勿整孔勾选。起点图标遮挡不在此工具修复范围。</p>
<p>此操作只补当前编号小孔，不连接开放断点、不删支叉、不推断通行顺序或GPS。放大原图并逐一对照后，才可选择白箭头等有明确证据的漏选。</p><img src="numbered.png">''' + ''.join(rows) + '''<p><label><input id="ack" type="checkbox">我已逐孔复核未叠加原图；所选孔确为漏选而非真实间隙</label></p><button id="save">下载选择文件 selection.json（不自动修改掩码）</button>
<script>document.getElementById('save').onclick=()=>{if(!document.getElementById('ack').checked){alert('须明确复核确认');return;}const r={fingerprint:FINGERPRINT,confirm_source_reviewed:true,fill_hole_ids:[...document.querySelectorAll('section input:checked')].map(x=>Number(x.value))};const u=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='selection.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};</script>'''.replace('FINGERPRINT',json.dumps(digest))
    (out/'review.html').write_text(page,encoding='utf-8')
    (out/'review.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    Image.fromarray(corrected.astype('uint8')*255).save(out/'mask-corrected.png')
    comparison = Image.new('RGB',(image.width*3,image.height))
    comparison.paste(image,(0,0))
    for j,m in enumerate((mask,corrected),1):
        rgb = np.array(image).copy(); rgb[m] = (rgb[m]*.4+np.array([255,0,80])*.6).astype('uint8')
        comparison.paste(Image.fromarray(rgb),(j*image.width,0))
    comparison.save(out/'comparison.png')
    before = analyze(thin(mask)[0]); after = analyze(thin(corrected)[0])
    result = {'verified':False,'status':'needs_confirmation','fingerprint':digest,
              'applied_hole_ids':ids,'added_pixels':int((corrected & ~mask).sum()),
              'before':{k:before[k] for k in ('component_count','cycle_rank','skeleton_pixels')},
              'after':{k:after[k] for k in ('component_count','cycle_rank','skeleton_pixels')},
              'warnings':['人工逐孔补全不是自动箭头识别；未选择孔及所有开放缺口保留。',
                          '组件、分支、环路及顺序仍需确认；未恢复完整路线、骑行方向或GPS。']}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-crop',required=True)
    p.add_argument('--mask',required=True)
    p.add_argument('--output-dir',required=True)
    p.add_argument('--selection',help='可视复核页下载的人工选择文件；不提供则只预览，不改像素')
    args = p.parse_args()
    try:
        print(json.dumps(review(args.source_crop,args.mask,args.output_dir,args.selection),ensure_ascii=False,indent=2))
    except (ValueError,OSError,Image.DecompressionBombError) as exc:
        p.exit(2,f'复核失败：{exc}\n')


if __name__ == '__main__':
    main()
