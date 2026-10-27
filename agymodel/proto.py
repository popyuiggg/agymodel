"""
轻量级无依赖 Protobuf Wire 格式解析器与通用字段解码工具。
"""

from typing import List, Tuple, Union

WireValue = Union[int, bytes]
ProtoField = Tuple[int, str, WireValue]


def parse_proto(data: bytes) -> List[ProtoField]:
    """
    解析 Protobuf 二进制流，返回 [(field_num, wire_type_name, value), ...]。
    wire_type_name 取值: 'varint' | 'bytes' | '64bit' | '32bit'
    """
    if not data:
        return []

    i = 0
    length = len(data)
    results: List[ProtoField] = []

    while i < length:
        # 读取 Tag (field_number << 3 | wire_type)
        shift = 0
        tag = 0
        while True:
            if i >= length:
                return results
            b = data[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7

        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            break

        if wire_type == 0:  # Varint
            shift = 0
            val = 0
            while True:
                if i >= length:
                    break
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            results.append((field_num, "varint", val))

        elif wire_type == 2:  # Length-delimited (string / bytes / submessage)
            shift = 0
            val_len = 0
            while True:
                if i >= length:
                    break
                b = data[i]
                i += 1
                val_len |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if val_len < 0 or i + val_len > length:
                break
            results.append((field_num, "bytes", data[i : i + val_len]))
            i += val_len

        elif wire_type == 1:  # 64-bit fixed
            if i + 8 > length:
                break
            results.append((field_num, "64bit", data[i : i + 8]))
            i += 8

        elif wire_type == 5:  # 32-bit fixed
            if i + 4 > length:
                break
            results.append((field_num, "32bit", data[i : i + 4]))
            i += 4

        else:
            # 未知或弃用的 wire_type，终止当前层解析
            break

    return results


def parse_duration(data: bytes) -> float:
    """解析 google.protobuf.Duration (1=seconds, 2=nanos) 为秒数 (保留3位小数)。"""
    sec = 0
    nano = 0
    for fn, wt, val in parse_proto(data):
        if fn == 1 and wt == "varint" and isinstance(val, int):
            sec = val
        elif fn == 2 and wt == "varint" and isinstance(val, int):
            nano = val
    return round(sec + nano / 1e9, 3)


def parse_timestamp(data: bytes) -> float:
    """解析 google.protobuf.Timestamp (1=seconds, 2=nanos) 为 Unix 时间戳。"""
    sec = 0
    nano = 0
    for fn, wt, val in parse_proto(data):
        if fn == 1 and wt == "varint" and isinstance(val, int):
            sec = val
        elif fn == 2 and wt == "varint" and isinstance(val, int):
            nano = val
    return sec + nano / 1e9


def decode_utf8(data: bytes) -> str:
    """安全解码 UTF-8 字符串。"""
    if not isinstance(data, bytes):
        return ""
    return data.decode("utf-8", errors="ignore").strip()
