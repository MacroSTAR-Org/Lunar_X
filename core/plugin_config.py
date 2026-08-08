# -*- coding: utf-8 -*-
"""
插件私有配置

约定的插件目录形态：

    plugins/<name>/
        setup.py       入口（框架加载它）
        plugin.json    清单：元信息 + 配置项声明（schema），插件作者维护
        config.json    配置实际值，用户 / WebUI 维护，插件升级不应覆盖
        README.md      配置文档

schema 与 values 分开放是刻意的：升级插件时 plugin.json 被新版覆盖，
config.json 保留用户改过的值；没配过的项自动回落到 schema 里的 default。

本模块同时被 bot 端（core/bot.py）和 WebUI 端（webui.py）使用，
所以不能 import 任何 bot 相关的东西，保持零依赖。
"""
import json
import os
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_FILE = 'plugin.json'
CONFIG_FILE = 'config.json'


class _Missing:
    """区分「键不存在」和「键存在但值是 None」，后者不该被 default 覆盖"""


_MISSING = _Missing()

# schema 里允许的字段类型
VALID_TYPES = {
    'bool', 'string', 'secret', 'text',
    'int', 'number', 'select',
    'string_list', 'int_list',
}


# ---------------------------------------------------------------- 点号嵌套

def get_nested(data: Dict[str, Any], dotted_key: str, default=None):
    """按 "a.b.c" 取值，中途缺失返回 default"""
    cur = data
    for part in dotted_key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_nested(data: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """按 "a.b.c" 赋值，中途缺失的层级自动建成 dict"""
    parts = dotted_key.split('.')
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ---------------------------------------------------------------- 读取

def plugin_dir(plugins_root: str, name: str) -> Optional[str]:
    """返回插件目录（兼容禁用态的 d_ 前缀）；单文件插件返回 None"""
    for candidate in (name, 'd_' + name):
        path = os.path.join(plugins_root, candidate)
        if os.path.isdir(path):
            return path
    return None


def load_manifest(plugins_root: str, name: str) -> Dict[str, Any]:
    """读 plugin.json。不存在或坏掉都返回 {}，绝不抛异常——
    清单是可选的，没有它插件照样能跑，只是没有 WebUI 配置能力。"""
    d = plugin_dir(plugins_root, name)
    if not d:
        return {}
    path = os.path.join(d, MANIFEST_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_schema(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从清单里取出配置项声明，过滤掉不合法的条目"""
    raw = manifest.get('config')
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if not key or not isinstance(key, str):
            continue
        if item.get('type', 'string') not in VALID_TYPES:
            continue
        out.append(item)
    return out


def load_raw_config(plugins_root: str, name: str) -> Dict[str, Any]:
    """只读 config.json 里用户存的值，不做 default 合并"""
    d = plugin_dir(plugins_root, name)
    if not d:
        return {}
    path = os.path.join(d, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def merge_defaults(schema: List[Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, Any]:
    """以 schema 的 default 打底，用户配过的键覆盖上去。

    只处理 schema 里声明过的键；config.json 里多出来的键原样保留
    （插件可能有不想暴露给 WebUI 的内部状态）。
    """
    out = json.loads(json.dumps(values)) if values else {}
    for item in schema:
        key = item['key']
        if get_nested(out, key, _MISSING) is _MISSING:
            set_nested(out, key, item.get('default'))
    return out


def load_config(plugins_root: str, name: str) -> Dict[str, Any]:
    """插件配置的最终形态：default 打底 + 用户值覆盖"""
    manifest = load_manifest(plugins_root, name)
    schema = get_schema(manifest)
    return merge_defaults(schema, load_raw_config(plugins_root, name))


# ---------------------------------------------------------------- 写入与校验

def coerce(value: Any, item: Dict[str, Any]) -> Tuple[bool, Any, str]:
    """把前端传来的值按 schema 声明的类型规范化。

    返回 (是否合法, 规范化后的值, 错误信息)。
    刻意做得宽松：表单里数字常常以字符串形式传上来，能转就转，
    真转不了才报错。
    """
    t = item.get('type', 'string')
    label = item.get('label') or item.get('key')

    try:
        if t == 'bool':
            if isinstance(value, bool):
                return True, value, ''
            if isinstance(value, str):
                return True, value.strip().lower() in ('true', '1', 'yes', 'on'), ''
            return True, bool(value), ''

        if t in ('string', 'secret', 'text'):
            return True, '' if value is None else str(value), ''

        if t == 'int':
            v = int(float(value))
            return _check_range(v, item, label)

        if t == 'number':
            v = float(value)
            return _check_range(v, item, label)

        if t == 'select':
            allowed = [o.get('value') for o in item.get('options', []) if isinstance(o, dict)]
            if allowed and value not in allowed:
                return False, None, f'{label}: 取值必须是 {allowed} 之一'
            return True, value, ''

        if t == 'string_list':
            return True, [str(x).strip() for x in _as_list(value) if str(x).strip()], ''

        if t == 'int_list':
            out = []
            for x in _as_list(value):
                s = str(x).strip()
                if not s:
                    continue
                out.append(int(float(s)))
            return True, out, ''

    except (TypeError, ValueError):
        return False, None, f'{label}: 需要 {t} 类型，收到 {value!r}'

    return False, None, f'{label}: 未知类型 {t}'


def _check_range(v, item, label):
    if 'min' in item and v < item['min']:
        return False, None, f'{label}: 不能小于 {item["min"]}'
    if 'max' in item and v > item['max']:
        return False, None, f'{label}: 不能大于 {item["max"]}'
    return True, v, ''


def _as_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    # 允许前端直接传多行文本或逗号分隔
    return [p for p in str(value).replace('，', ',').replace('\n', ',').split(',')]


def validate_and_build(schema: List[Dict[str, Any]],
                       incoming: Dict[str, Any],
                       existing: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], List[str]]:
    """按 schema 校验并生成要落盘的配置。

    安全要点：**只接受 schema 里声明过的 key**。前端传来的其他键一律丢弃，
    避免通过配置接口往插件目录里写任意内容。
    existing 里未被 schema 覆盖的键会保留（插件的内部状态不被抹掉）。
    """
    errors: List[str] = []
    out = json.loads(json.dumps(existing)) if existing else {}

    for item in schema:
        key = item['key']
        if key not in incoming:
            continue                      # 没传的项保持原值
        ok, val, err = coerce(incoming[key], item)
        if not ok:
            errors.append(err)
            continue
        set_nested(out, key, val)

    return (not errors), out, errors


def save_config(plugins_root: str, name: str, data: Dict[str, Any]) -> None:
    """写 config.json。目录不存在会抛异常，由调用方处理。"""
    d = plugin_dir(plugins_root, name)
    if not d:
        raise FileNotFoundError(f'插件 {name} 不是目录形态，无法保存配置')
    path = os.path.join(d, CONFIG_FILE)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)                 # 原子替换，避免写一半被读到


def flatten_for_form(schema: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """把嵌套配置拍平成 {点号key: 值}，方便前端按 schema 逐项绑定"""
    return {item['key']: get_nested(config, item['key'], item.get('default'))
            for item in schema}
