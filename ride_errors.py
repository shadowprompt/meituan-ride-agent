"""固定错误代码与恢复文案；不按异常文本猜原因，不公开底层响应。"""
import socket
import urllib.error

GUIDANCE = {
    'image_input_invalid': '截图解码或格式/大小校验未通过。仅支持实际编码为 PNG/JPEG/WebP、20 MB 内且不超过400万像素的图片；后缀不是格式证据。请在本机转换或缩小后新建，不会自动放宽校验。',
    'api_configuration_missing': '网页服务进程至少缺少 RIDE_API_KEY 或 RIDE_MODEL 中一项（或值为空），尚未调用模型。请在启动网页的同一终端正确设置并导出配置后重启网页；不会自动读取 .env，不要发送密钥。',
    'api_configuration': '模型服务配置未通过本地校验。请管理员核对 RIDE_API_BASE、RIDE_API_KEY、RIDE_MODEL；不要发送密钥或完整环境文件。',
    'api_http': '模型服务返回 HTTP 错误。请核对权限、模型、额度与服务状态后重新授权；不是颜色或裁剪错误，远端响应正文不公开。',
    'api_response_invalid': '模型服务响应不是要求的 JSON 动作结构。请检查模型是否支持视觉和结构化指令后重新授权；不会自动修复或执行响应代码。',
    'model_action_invalid': '模型返回的动作或参数未通过校验。请检查模型能力并重新授权重试；未产生有效提取不代表颜色或裁剪一定错误。',
    'extraction_failed': '本地提取步骤失败。请核对截图与模型给出的颜色、裁剪参数；也可能是本地文件或处理故障，请勿据此认定颜色选错。',
    'inspection_failed': '本地颜色检查步骤失败。请核对裁剪参数和本地文件；此错误不证明截图没有轨迹。',
    'centerline_failed': '本地中心线处理失败。请检查当前掩码和本地文件；不能据此直接判断需要更换颜色。',
    'registration_failed': '本地配准步骤失败。请核对当前裁剪对应的控制点与独立检查点文件。',
}


class TaskFailure(ValueError):
    def __init__(self, category, message=None):
        self.category = category
        super().__init__(message or GUIDANCE.get(category, '任务处理失败；底层错误不公开。'))


def failure_category(exc, stage=None):
    """类型和明确执行阶段是证据；缺少产物不是错误原因。"""
    if isinstance(exc, TaskFailure):
        return exc.category
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(getattr(exc, 'reason', None), (TimeoutError, socket.timeout)):
        return 'api_timeout' if stage == 'planner' else 'task_processing_failed'
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return 'api_network' if stage == 'planner' else 'task_processing_failed'
    return {'extract':'extraction_failed', 'inspect_colors':'inspection_failed',
            'centerline':'centerline_failed', 'register':'registration_failed',
            'match':'road_processing_failed', 'fetch_map':'road_processing_failed',
            'action':'model_action_invalid'}.get(stage, 'task_processing_failed')
