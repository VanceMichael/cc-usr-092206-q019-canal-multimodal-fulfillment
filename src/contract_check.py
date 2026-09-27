"""零依赖的 JSON Schema 最小子集校验器。

仅支持本仓库契约实际用到的关键字：$ref/$defs、type（含 ["x","null"]）、
required、properties、items、enum、minimum、minLength、minItems、
additionalProperties:false。避免引入第三方依赖。
"""
from __future__ import annotations

from typing import Any

_TYPE_MAP = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


class SchemaError(Exception):
    pass


def validate(instance: Any, schema: dict, defs: dict | None = None,
             path: str = "$") -> None:
    defs = defs or schema.get("$defs", {})
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise SchemaError(f"暂不支持的引用 {ref}")
        validate(instance, defs[ref.split("/")[-1]], defs, path)
        return

    allowed = schema.get("enum")
    if allowed is not None and instance not in allowed:
        raise SchemaError(f"{path} 值 {instance!r} 不在 {allowed} 中")

    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        pytypes = tuple(t for ty in types for t in
                        ([_TYPE_MAP[ty]] if not isinstance(_TYPE_MAP[ty], tuple)
                         else _TYPE_MAP[ty]))
        # bool 不应被当作 number/integer
        if isinstance(instance, bool) and \
                ("number" in types or "integer" in types) and \
                "boolean" not in types:
            raise SchemaError(f"{path} 类型不匹配")
        if not isinstance(instance, pytypes) or \
                (isinstance(instance, bool) and "boolean" not in types
                 and ("number" in types or "integer" in types)):
            raise SchemaError(f"{path} 应为 {types}，实际 {type(instance).__name__}")
        if "integer" in types and isinstance(instance, int) and \
                not isinstance(instance, bool):
            pass

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path} 短于 {schema['minLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path} 小于最小值 {schema['minimum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path} 少于 {schema['minItems']} 项")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                validate(item, item_schema, defs, f"{path}[{i}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"{path} 缺少必需字段 {key}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props) - set(schema.get("required", []))
            # required 字段也应在 properties 中；只允许 properties 列出的键
            bad = set(instance) - set(props)
            if bad:
                raise SchemaError(f"{path} 出现未声明字段 {sorted(bad)}")
        for key, subschema in props.items():
            if key in instance:
                validate(instance[key], subschema, defs, f"{path}.{key}")
