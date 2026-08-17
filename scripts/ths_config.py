"""
thsdk 账户配置共享模块
所有需要同花顺账户的程序统一从这里读取配置。

用法:
    from ths_config import get_ths_ops
    from thsdk import THS
    with THS(get_ths_ops()) as ths:
        ...
"""
import json
from pathlib import Path

_CONF_CACHE = None


def get_ths_ops(cfg_path: str = None) -> dict:
    """
    读取 thsdk 登录配置。
    
    参数:
        cfg_path: 指定 ths_config.json 路径，默认自动搜索
    
    返回:
        dict: {'username': 'xxx', 'password': 'xxx'}，找不到配置则返回 {}
    """
    global _CONF_CACHE
    if _CONF_CACHE is not None:
        return _CONF_CACHE
    
    search_paths = []
    if cfg_path:
        search_paths.append(Path(cfg_path))
    
    # 自动搜索路径
    base = Path(__file__).parent.resolve()
    search_paths.extend([
        base / 'ths_config.json',                    # 同目录
        base.parent / 'workspace' / 'ths_config.json',  # workspace
        Path.cwd() / 'ths_config.json',              # 当前运行目录
    ])
    
    for p in search_paths:
        if p.exists():
            try:
                cfg = json.loads(p.read_text())
                if cfg.get('username') and cfg.get('password'):
                    _CONF_CACHE = cfg
                    return cfg
            except:
                continue
    
    return {}


def format_ths_ops() -> str:
    """返回格式化字符串，方便打印验证"""
    ops = get_ths_ops()
    user = ops.get('username', '')
    pwd = ops.get('password', '')
    if user and pwd:
        return f"account:{user} ✓"
    return "guest mode (no config)"
