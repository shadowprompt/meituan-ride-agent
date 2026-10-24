"""人工路线页面：复用 ride_web 的回环、CSRF 与任务根目录。"""
PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>本地有序像素路线</title>
<style>body{font:16px sans-serif;max-width:1100px;margin:24px auto;padding:12px}button,input{font:inherit;margin:6px;padding:6px}canvas{display:block;max-width:100%;height:auto;cursor:crosshair}#viewport{overflow:auto;max-height:75vh;border:1px solid #888}#error{color:#a00;white-space:pre-wrap}#status{white-space:pre-wrap}label{display:inline-block}a{margin:8px}</style>
<h1>人工有序像素路线 · 本地第一阶段</h1><p><a href="/">原有自动提取 / 配准入口</a></p>
<p>无需 API 配置或上传授权。截图只保存在本机，不上传、不下载地图。自动骨架断裂或歧义时，按<strong>实际经过顺序逐点点击</strong>；重复经过同一坐标或线段允许保留。不吸附、不算最短路、不推断 GPS。</p>
<p>这是人工近似折线，起点是第 1 点，终点是最后一点；箭头仅表示所点顺序，未经真实行程验证。不是可导航 GPX，不含时间或速度。图像按 EXIF 转正，坐标绑定转正后的原图。</p>
<label>原图 PNG/JPEG/WebP <input id="image" type="file" accept="image/png,image/jpeg,image/webp"></label><button id="create">上传并新建本地人工任务</button>
<div><label>任务编号 <input id="task" size="40" autocomplete="off"></label><button id="open">重新打开已保存任务</button></div>
<p id="status" role="status">尚未打开任务</p><p id="error" role="alert"></p>
<fieldset id="editor" disabled><legend>按经过顺序标注</legend>
<label>显示裁剪 [左,上,右,下] <input id="crop" size="26"></label><button id="applyCrop">应用显示裁剪（不改点坐标）</button><button id="full">显示整张原图</button>
<label>缩放 <input id="zoom" type="range" min="25" max="200" step="25" value="100"></label>
<div id="viewport"><canvas id="canvas" aria-label="原图有序路线点击标注区"></canvas></div>
<p>图像自适应缩放；点坐标始终存原图像素，显示裁剪左上偏移自动加回。裁剪仅改变视图，已有范围外点不删除；播放为逐段顺序示意，等间隔不是骑行时间。重复点产生零长度段时只显示编号。</p>
<button id="undo">撤销最后一点</button><button id="clear">清空全部点（需确认）</button><button id="play">播放顺序</button><button id="stop">停止播放</button><button id="save">保存有序路线</button><a id="export" hidden download="ordered-pixel-route.json">导出已保存像素路线 JSON</a>
<p id="count"></p><details><summary>检查有序原图坐标（可重复）</summary><pre id="points"></pre></details>
</fieldset>
<script nonce="__TOKEN__">
'use strict';
const token='__TOKEN__',$=id=>document.getElementById(id);let state=null,image=null,points=[],crop=[],dirty=false,timer=null,playIndex=null,busy=false,epoch=0;
async function request(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':token}:{},body:data?JSON.stringify(data):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d;}
function path(){const id=$('task').value.trim();if(!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(id))throw Error('请输入有效任务编号');return '/api/pixel-routes/'+id;}
function stop(){if(timer)clearInterval(timer);timer=null;playIndex=null;}
function mark(){dirty=true;$('export').hidden=true;draw();}
function draw(){if(!image)return;const c=$('canvas'),ctx=c.getContext('2d'),[l,t,r,b]=crop;c.width=r-l;c.height=b-t;c.style.width=(c.width*Number($('zoom').value)/100)+'px';ctx.drawImage(image,l,t,r-l,b-t,0,0,c.width,c.height);ctx.save();ctx.beginPath();ctx.rect(0,0,c.width,c.height);ctx.clip();const limit=playIndex===null?points.length:playIndex;
for(let i=0;i<limit;i++){const p=points[i],x=p[0]-l,y=p[1]-t;if(i){const a=points[i-1],ax=a[0]-l,ay=a[1]-t,dx=x-ax,dy=y-ay,n=Math.hypot(dx,dy);ctx.strokeStyle='#e00060';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(x,y);ctx.stroke();if(n>0){const mx=(ax+x)/2,my=(ay+y)/2,ux=dx/n,uy=dy/n;ctx.fillStyle='#e00060';ctx.beginPath();ctx.moveTo(mx+ux*7,my+uy*7);ctx.lineTo(mx-ux*6-uy*5,my-uy*6+ux*5);ctx.lineTo(mx-ux*6+uy*5,my-uy*6-ux*5);ctx.closePath();ctx.fill();}}
ctx.fillStyle=i===0?'#087f23':i===points.length-1?'#b00020':'#003aaa';ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();ctx.font='bold 14px sans-serif';ctx.lineWidth=3;ctx.strokeStyle='white';const label=(i===0?'起点 ':i===points.length-1?'终点 ':'')+(i+1);ctx.strokeText(label,x+6,y-6);ctx.fillText(label,x+6,y-6);}
ctx.restore();$('count').textContent=points.length+' 个有序点 / '+Math.max(0,points.length-1)+' 段（重复保留）'+(dirty?' · 有未保存修改':' · 已保存')+(playIndex!==null?' · 播放至第 '+playIndex+' 点':'');$('points').textContent=JSON.stringify(points);}
async function install(d){const version=++epoch;const img=new Image();await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(Error('本地图像加载失败'));img.src=d.image_url;});if(version!==epoch)return;stop();state=d;image=img;points=d.route.points.map(p=>p.slice());crop=d.route.view_crop.slice();dirty=false;$('task').value=d.task_id;$('crop').value=JSON.stringify(crop);$('editor').disabled=false;$('export').href=d.export_url;$('export').hidden=points.length<2;$('status').textContent='本地任务 '+d.task_id+' · 保存版本 '+d.route.revision+' · 人工近似 / 未验证';localStorage.setItem('ride-manual-task',d.task_id);draw();}
async function action(fn){if(busy)return;busy=true;$('error').textContent='';$('editor').disabled=true;try{await fn();}catch(e){$('error').textContent=e.message;}finally{busy=false;$('editor').disabled=!state;}}
function discard(){return !dirty||confirm('存在未保存标注，放弃修改并重新打开/新建任务？');}
$('create').onclick=()=>action(async()=>{if(!discard())return;const f=$('image').files[0];if(!f||f.size>20000000)throw Error('请选择 ≤20 MB 图片');const b64=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(f);});await install(await request('/api/pixel-routes',{image_base64:b64}));});
$('open').onclick=()=>action(async()=>{if(discard())await install(await request(path()));});
$('canvas').onclick=e=>{if(!state||busy)return;stop();if(points.length>=10000){$('error').textContent='最多 10000 点';return;}const rect=$('canvas').getBoundingClientRect();const x=crop[0]+(e.clientX-rect.left)*$('canvas').width/rect.width,y=crop[1]+(e.clientY-rect.top)*$('canvas').height/rect.height;points.push([Math.round(Math.max(0,Math.min(image.width-1,x))*1000)/1000,Math.round(Math.max(0,Math.min(image.height-1,y))*1000)/1000]);mark();};
$('undo').onclick=()=>{stop();if(points.length){points.pop();mark();}};
$('clear').onclick=()=>{if(points.length&&confirm('确认清空全部有序点？保存后将覆盖当前路线，不能自动找回。')){stop();points=[];mark();}};
$('applyCrop').onclick=()=>{try{const c=JSON.parse($('crop').value);if(!Array.isArray(c)||c.length!==4||!c.every(Number.isInteger)||!(0<=c[0]&&c[0]<c[2]&&c[2]<=image.width&&0<=c[1]&&c[1]<c[3]&&c[3]<=image.height))throw Error();stop();crop=c;mark();$('error').textContent='';}catch(e){$('error').textContent='裁剪须为原图范围内的整数 [左,上,右,下]';}};
$('full').onclick=()=>{stop();crop=[0,0,image.width,image.height];$('crop').value=JSON.stringify(crop);mark();};$('zoom').oninput=draw;
$('save').onclick=()=>action(async()=>{stop();const p='/api/pixel-routes/'+state.task_id;const d=await request(p,{image:state.route.image,revision:state.route.revision,points,view_crop:crop});await install(d);});
$('play').onclick=()=>{stop();if(!points.length)return;playIndex=1;draw();timer=setInterval(()=>{if(playIndex>=points.length){stop();draw();return;}playIndex++;draw();},500);};$('stop').onclick=()=>{stop();draw();};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
const last=localStorage.getItem('ride-manual-task');if(last){$('task').value=last;action(async()=>{await install(await request(path()));});}
</script></html>'''
