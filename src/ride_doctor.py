"""本地交付预检：只检查运行条件，不发送截图、不调用远端、不打印密钥。"""
import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import urllib.parse


def diagnose():
    checks=[]
    def add(name,ok,detail,required=True):
        checks.append(dict(name=name,ok=bool(ok),detail=detail,required=required))
    add('python',sys.version_info >= (3,10),'需要 Python 3.10 或更高版本')
    for module in ('PIL','numpy'):
        try:
            importlib.import_module(module)
            add(module,True,'已安装')
        except ImportError:
            add(module,False,'请运行 python -m pip install -r requirements.txt')
    root=Path(__file__).resolve().parent.parent
    for filename in ('src/ride_agent.py','src/ride_web.py','requirements.txt'):
        add(filename,(root/filename).is_file(),'交付文件存在性')
    # 只报告配置是否具备，不回显供应商地址、用户名、密钥或模型值。
    key=bool(os.environ.get('RIDE_API_KEY'))
    model=bool(os.environ.get('RIDE_MODEL'))
    base=os.environ.get('RIDE_API_BASE','https://api.openai.com/v1')
    parsed=urllib.parse.urlparse(base)
    valid=bool(parsed.hostname) and not any((parsed.username,parsed.password,parsed.query,parsed.fragment))
    valid=valid and (parsed.scheme=='https' or (parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')))
    add('vision_key',key,'视觉 API 密钥已配置' if key else '未配置 RIDE_API_KEY；只能离线演示',False)
    add('vision_model',model,'模型名已配置' if model else '未配置 RIDE_MODEL；只能离线演示',False)
    add('vision_url',valid,'API 地址格式检查；未执行连通性测试',False)
    local_ready=all(c['ok'] for c in checks if c['required'])
    return dict(local_ready=local_ready,vision_configured=bool(key and model and valid),
                live_vision_verified=False,real_screenshot_acceptance=False,
                checks=checks,notice='配置存在不等于模型联调通过；本命令没有网络调用。')


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    report=diagnose()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['local_ready'] else 1


if __name__=='__main__':
    raise SystemExit(main())
