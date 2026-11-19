"""核心自测：不需要串口和 DTU。

用法：
    .venv\\Scripts\\python.exe selftest.py
"""

from __future__ import annotations

from pathlib import Path
import tempfile

from app.device_model import DeviceModel, format_hex, parse_hex
from app.fault_injector import FaultConfig, FaultInjector
from app.frame_codec import FrameCodec
from app.protocol_engine import ProtocolEngine
from app.template_store import (
    load_templates,
    save_templates,
    template_key_for_frame,
)

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

    print("== 故障注入 ==")
    base = parse_hex(REAL_READ_FRAMES[0])
    injector = FaultInjector()
    result = injector.apply(base, FaultConfig(), 1)
    check(
        result.data == base and result.delay_ms == 0 and not result.notes,
        "正常响应不变",
    )
    result = injector.apply(base, FaultConfig(drop_all=True), 1)
    check(result.data is None, "不回帧")
    injector.reset()
    first = injector.apply(base, FaultConfig(drop_every_n=2), 1)
    second = injector.apply(base, FaultConfig(drop_every_n=2), 1)
    check(first.data is not None and second.data is None, "每 2 帧丢 1 帧")
    result = injector.apply(base, FaultConfig(bad_checksum=True), 1)
    check(
        result.data is not None and result.data[-1] == (base[-1] ^ 0xFF),
        "错校验",
    )
    result = injector.apply(base, FaultConfig(wrong_address=True), 1)
    check(
        result.data is not None
        and result.data[3] == 2
        and (sum(result.data[:-1]) & 0xFF) == result.data[-1],
        "错地址且校验有效",
    )
    result = injector.apply(base, FaultConfig(short_frame=True), 1)
    check(
        result.data is not None and len(result.data) == len(base) - 1,
        "短帧少 1 字节",
    )
    result = injector.apply(base, FaultConfig(delay_ms=200), 1)
    check(result.delay_ms == 200, "延迟参数")

    print("== 模板键/存储 ==")
    check(DeviceModel.parse_group_key("27/1") == (0x1B, 0x01), "27/1 按十进制解析")
    check(
        DeviceModel.parse_group_key("1B/01") == (0x1B, 0x01),
        "1B/01 按十六进制解析",
    )
    check(
        DeviceModel.parse_group_key("0x1B/0x01") == (0x1B, 0x01),
        "0x1B/0x01 按十六进制解析",
    )
    response_frame = FrameCodec.extract(
        bytearray(parse_hex(REAL_READ_FRAMES[0]))
    )[0]
    check(template_key_for_frame(response_frame) == "0x1B01", "0x1B 响应映射为 0x1B01")
    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = Path(temp_dir) / "templates.json"
        save_templates(
            template_path,
            {"0x1B02": "00 5A 08 66", "0x1B01": "00 18 12 5C"},
        )
        check(
            load_templates(template_path)
            == {"0x1B01": "00 18 12 5C", "0x1B02": "00 5A 08 66"},
            "模板保存/加载",
        )
        template_path.write_text("{}", encoding="utf-8")
        check(load_templates(template_path) == {}, "空模板文件返回空字典")

    print("== 多组定值读写 ==")
    multi_xml = """<?xml version="1.0" encoding="GB2312"?>
<IEDDefine>
<FixValDefine Count="4">
<FixValDefine0 Id="0" GroupNo="27" EntryNo="1" FUN="0" INF="2" Name="电池容量" ValueType="4" Enable="1"/>
<FixValDefine1 Id="1" GroupNo="27" EntryNo="1" FUN="2" INF="2" Name="截止电压" ValueType="6" Enable="1"/>
<FixValDefine2 Id="2" GroupNo="27" EntryNo="2" FUN="0" INF="2" Name="周期" ValueType="4" Enable="1"/>
<FixValDefine3 Id="3" GroupNo="27" EntryNo="2" FUN="2" INF="2" Name="剩余" ValueType="4" Enable="1"/>
</FixValDefine>
</IEDDefine>
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        multi_path = Path(temp_dir) / "multi.xml"
        multi_path.write_text(multi_xml, encoding="gb18030")
        multi_device = DeviceModel()
        multi_device.load_xml(multi_path)
        check(len(multi_device.groups) == 2, "合成 XML 有 2 个定值组")
        multi_device.apply_templates(
            {"0x1B01": "00 18 12 5C", "0x1B02": "00 5A 08 66"}
        )
        multi_engine = ProtocolEngine(multi_device)

        read_1 = FrameCodec.build(1, 0x1B, 0x01)
        read_2 = FrameCodec.build(1, 0x1B, 0x02)
        response_1 = multi_engine.handle(FrameCodec.extract(bytearray(read_1))[0])
        response_2 = multi_engine.handle(FrameCodec.extract(bytearray(read_2))[0])
        check(
            response_1
            == FrameCodec.build(1, 0x1B, 0x01, parse_hex("00 18 12 5C")),
            "0x1B01 读响应",
        )
        check(
            response_2
            == FrameCodec.build(1, 0x1B, 0x02, parse_hex("00 5A 08 66")),
            "0x1B02 读响应",
        )

        write_1 = FrameCodec.build(1, 0x1A, 0x01, parse_hex("00 1E 12 C0"))
        write_2 = FrameCodec.build(1, 0x1A, 0x02, parse_hex("00 3C 00 64"))
        response_1 = multi_engine.handle(FrameCodec.extract(bytearray(write_1))[0])
        response_2 = multi_engine.handle(FrameCodec.extract(bytearray(write_2))[0])
        check(response_1 == write_1, "0x1A01 写入回显")
        check(response_2 == write_2, "0x1A02 写入回显")
        check(
            format_hex(bytes(multi_device.find(0x1B, 0x01).data))
            == "00 1E 12 C0",
            "0x1A01 写入后组数据更新",
        )
        check(
            format_hex(bytes(multi_device.find(0x1B, 0x02).data))
            == "00 3C 00 64",
            "0x1A02 写入后组数据更新",
        )

    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())