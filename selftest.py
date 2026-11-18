"""核心自测：不需要串口和 DTU。

用法：
    .venv\\Scripts\\python.exe selftest.py
"""

from __future__ import annotations

from pathlib import Path

from app.device_model import DeviceModel, format_hex, parse_hex
from app.frame_codec import FrameCodec
from app.protocol_engine import ProtocolEngine
from app.template_store import load_templates

XML_PATH = Path(r"E:\充电机\充电机参考装置类型表\DMP2219_Pow_CQ1000WTJND.xml")
FIX_XML_PATH = Path(
    r"E:\充电机\增加定值读写\备份\mnt\Config\IEDDefine\WTJND.xml"
)
TEMPLATES_PATH = Path(__file__).resolve().parent / "data" / "templates.json"

# 用户用实物抓到的 5 组 0x1B01 报文（第 2/4/5 组与第 1/3 组重复）
REAL_READ_FRAMES = [
    "EB 90 0B 01 1B 01 00 18 12 5C 00 28 00 64 B5",
    "EB 90 0B 01 1B 01 00 18 12 5C 00 28 00 64 B5",
    "EB 90 0B 01 1B 01 00 18 12 C0 00 32 00 64 23",
    "EB 90 0B 01 1B 01 00 18 12 C0 00 32 00 64 23",
    "EB 90 0B 01 1B 01 00 18 12 5C 00 28 00 64 B5",
]

REQUEST_READ_0B01 = "EB 90 03 01 1B 01 9B"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  OK  {message}")


def main() -> int:
    print("== 加载模型 ==")
    check(XML_PATH.exists(), f"XML 存在: {XML_PATH}")
    device = DeviceModel()
    device.load_xml(XML_PATH)
    templates = load_templates(TEMPLATES_PATH)
    device.apply_templates(templates)
    # 9 个 XML 组 + 1 个由 templates.json 生成的 0x1B01 组
    check(len(device.groups) == 10, f"组数 = {len(device.groups)}")
    check(
        sum(len(g.points) for g in device.groups.values()) == 28,
        "点数 = 28（4 遥测 + 19 遥信 + 5 遥控）",
    )

    group = device.find(0x1B, 0x01)
    check(group is not None, "存在 0x1B/01 定值组")
    assert group is not None
    check(len(group.data) == 8, f"0x1B/01 数据区长度 = {len(group.data)}")
    check(
        format_hex(bytes(group.data)) == "00 18 12 5C 00 28 00 64",
        "模板初始化为 24AH / 47.00V / 40% / 100%",
    )

    print("== 回放 5 组真实 0x1B01 响应 ==")
    codec = FrameCodec()
    engine = ProtocolEngine(device)
    request_buf = bytearray(parse_hex(REQUEST_READ_0B01))
    requests = FrameCodec.extract(request_buf)
    check(len(requests) == 1 and requests[0].valid, "请求帧解析通过")
    request = requests[0]

    # 按真实帧的数据区设置组数据，再让引擎生成响应并逐字节比较
    for index, real_hex in enumerate(REAL_READ_FRAMES, 1):
        real = parse_hex(real_hex)
        group.data = bytearray(real[6:-1])
        response = engine.handle(request)
        assert response is not None
        check(
            format_hex(response) == real_hex,
            f"第 {index} 组真实帧逐字节一致",
        )

    print("== 写入回显 0x1A01 ==")
    write_request = parse_hex("EB 90 09 01 1A 01 00 18 12 5C 00 28 4E")
    write_frames = FrameCodec.extract(bytearray(write_request))
    check(len(write_frames) == 1 and write_frames[0].valid, "写请求帧解析通过")
    response = engine.handle(write_frames[0])
    assert response is not None
    expected_write = "EB 90 0B 01 1A 01 00 18 12 5C 00 28 00 64 B4"
    check(format_hex(response) == expected_write, "写响应原样回显 8 字节数据区")

    print("== 控制/查询 ==")
    control_request = FrameCodec.extract(bytearray(parse_hex("EB 90 03 01 02 01 82")))[0]
    response = engine.handle(control_request)
    assert response is not None
    check(
        format_hex(response) == "EB 90 04 01 82 01 01 04",
        "0x02/01 控制成功应答",
    )

    query_request = FrameCodec.extract(bytearray(parse_hex("EB 90 03 01 01 01 81")))[0]
    response = engine.handle(query_request)
    assert response is not None
    check(
        format_hex(response) == "EB 90 07 01 81 01 00 00 00 00 05",
        "0x01/01 查询应答（数据区 4 字节）",
    )

    print("== 异常帧 ==")
    bad = bytearray(parse_hex("EB 90 03 01 1B 01 00"))
    frames = FrameCodec.extract(bad)
    check(len(frames) == 1 and not frames[0].valid, "错误校验和能被识别")

    partial = bytearray(parse_hex("EB 90 03 01 1B"))
    check(len(FrameCodec.extract(partial)) == 0, "半帧不会被误解析")
    partial.extend(parse_hex("01 9B"))
    check(len(FrameCodec.extract(partial)) == 1, "补齐后能解析")

    print("== 所有组请求分发 ==")
    for group in device.sorted_groups():
        if group.kind == "YK" and group.group_no == 0x1A:
            request_hex = FrameCodec.build(1, 0x1A, group.entry_no, b"\xFF\xFF")
        else:
            request_hex = FrameCodec.build(1, group.group_no, group.entry_no)
        group_requests = FrameCodec.extract(bytearray(request_hex))
        assert len(group_requests) == 1 and group_requests[0].valid
        response = engine.handle(group_requests[0])
        check(
            response is not None,
            f"组 0x{group.fun_type:04X}（{group.kind}）有应答",
        )

    print("== 当前定值表 WTJND.xml ==")
    if FIX_XML_PATH.exists():
        fix_device = DeviceModel()
        fix_device.load_xml(FIX_XML_PATH)
        check(len(fix_device.groups) == 1, "定值表只有 0x1B01 一个组")
        fix_group = fix_device.find(0x1B, 0x01)
        check(fix_group is not None, "存在 0x1B/01")
        assert fix_group is not None
        check(len(fix_group.points) == 3, "XML 里 3 个定值点")
        check(
            [p.name for p in fix_group.points]
            == ["电池容量", "活化放电截止电压", "电池剩余报警容量"],
            "定值点名称正确",
        )
        fix_device.apply_templates(templates)
        check(len(fix_group.data) == 8, "模板把数据区扩展到 8 字节（含剩余容量）")
        fix_engine = ProtocolEngine(fix_device)
        response = fix_engine.handle(request)
        assert response is not None
        check(
            format_hex(response) == REAL_READ_FRAMES[0],
            "WTJND.xml + 模板 回出的 0x1B01 与实物帧一致",
        )
    else:
        print(f"  SKIP 未找到 {FIX_XML_PATH}")

    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())