"""Portable named saves. Names are labels, not online credentials."""
import hashlib
import json
import os
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from PySide6.QtCore import QLockFile
from . import config


def username(value):
    name = unicodedata.normalize('NFKC', value or '').strip()
    if not 1 <= len(name) <= 32 or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('用户名请填写 1～32 个字符，不能包含换行或控制字符。')
    if any(c in name for c in '/\\:*?"<>|') or name in ('.', '..'):
        raise ValueError('用户名不能包含 / \\ : * ? " < > | 等字符。')
    return name


@dataclass
class ProfileSession:
    name: str
    directory: Path
    config: config.Config
    lock: QLockFile

    def release(self):
        self.lock.unlock()


class ProfileStore:
    def __init__(self, root=None):
        # Explicit test home never creates real portable saves.
        base = root or os.environ.get('RESOURCE_FINDER_HOME') or config.app_dir()
        self.root = Path(base).resolve() / 'profiles'

    def directory(self, name):
        key = username(name).casefold().encode('utf-8')
        path = self.root / hashlib.sha256(key).hexdigest()
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError('存档目录不能使用符号链接，请检查 profiles 文件夹。')
        return path

    def names(self):
        if not self.root.exists():
            return []
        rows = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                name = username(json.loads((directory/'profile.json').read_text(encoding='utf-8'))['name'])
                if self.directory(name) == directory:
                    rows.append(name)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(rows, key=str.casefold)

    def exists(self, name):
        return (self.directory(name)/'profile.json').is_file()

    def legacy_path(self):
        override = os.environ.get('RESOURCE_FINDER_HOME')
        candidates = ([Path(override)/'config.json'] if override else [
            Path(config._appdata())/config.APP_ID/'config.json', Path(config.app_dir())/'data/config.json'])
        return next((p for p in candidates if p.is_file()), None)

    def open(self, raw_name, import_legacy=False):
        name = username(raw_name)
        path = self.directory(name)
        path.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(path/'.session.lock'))
        if not lock.tryLock(0):
            raise ValueError('这个存档正在另一个窗口中使用，请先关闭该窗口再进入。')
        try:
            if self.exists(name):
                metadata = json.loads((path/'profile.json').read_text(encoding='utf-8'))
                name = username(metadata['name'])
                if self.directory(name) != path:
                    raise ValueError('存档名称信息不匹配。')
                data = json.loads((path/'config.json').read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('配置格式损坏，请保留存档并恢复备份。')
                cfg = config.Config.from_dict(data)
            else:
                cfg = config.Config()
                if import_legacy:
                    legacy = self.legacy_path()
                    if not legacy:
                        raise ValueError('没有找到可导入的旧版配置。')
                    data = json.loads(legacy.read_text(encoding='utf-8'))
                    if not isinstance(data, dict):
                        raise ValueError('旧版配置格式不正确，无法导入。')
                    cfg = config.Config.from_dict(data)
                cfg._storage_dir = str(path)
                if not config.save_config(cfg):
                    raise OSError('无法写入存档，请将整个工具文件夹移到可写的位置后重试。')
                temporary = path/('profile-'+uuid.uuid4().hex+'.part')
                temporary.write_text(json.dumps({'name': name}, ensure_ascii=False), encoding='utf-8')
                os.replace(temporary, path/'profile.json')
            cfg._storage_dir, cfg._profile_name = str(path), name
            return ProfileSession(name, path, cfg, lock)
        except Exception:
            lock.unlock()
            raise
