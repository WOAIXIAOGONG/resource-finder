"""Validate a generated release directory before any destructive build/copy."""
from pathlib import Path
import os


def release_target(source, output, name='资源搜寻器'):
    if not name or name in ('.', '..') or any(c in name for c in '\\/:*?"<>|\0'):
        raise ValueError('程序名称只能是文件名，不能包含路径')
    source = Path(source).resolve()
    candidate = Path(output).resolve() / name
    if candidate.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(candidate)):
        raise ValueError('构建目录不能是符号链接或目录联接')
    target = candidate.resolve()
    protected = [source / item for item in (
        'app', 'tools', 'assets', 'vendor', 'browser-extension', 'download-login-helper',
        'shots', '.venv', '_internal', 'profiles', 'data', 'downloads')]
    if (source == target or source.is_relative_to(target) or
            any(target == p or target.is_relative_to(p) or p.is_relative_to(target) for p in protected)):
        raise ValueError('输出目录会覆盖源码、运行环境或用户数据，请指定独立构建目录')
    for name in ('profiles', 'data', 'downloads'):
        if (target / name).exists() or (target / name).is_symlink():
            raise ValueError('输出目录含用户存档或运行数据（%s），请指定独立空目录，不能覆盖或清理' % name)
    return target
