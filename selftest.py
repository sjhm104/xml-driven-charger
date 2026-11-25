"""核心自测：不需要串口和 DTU。

用法：
    .venv\\Scripts\\python.exe selftest.py
"""

from __future__ import annotations

from pathlib import Path
import struct
import tempfile

from app.battery_model import (
    BatteryConfig,
    BatteryModel,
    apply_point_values,
    apply_snapshot,
    load_battery_config,
    role_for_point,
    save_battery_config,
)
from app.device_model import DeviceModel, Point, format_hex, parse_hex
from app.fault_injector import FaultConfig, FaultInjector
from app.frame_codec import FrameCodec
from app.point_format import PointFormat
from app.point_format_store import load_formats, point_key, save_formats
from app.protocol_engine import ProtocolEngine
from app.protocol_defaults import apply_group_defaults, defaults_for_xml
from app.preset_store import apply_preset, collect_preset, load_preset, save_preset
from app.xml_validator import validate_device

XML_PATH = Path(r"E:\充电机\充电机参考装置类型表\DMP2219_Pow_CQ1000WTJND.xml")
FIX_XML_PATH = Path(
    r"E:\充电机\增加定值读写\备份\mnt\Config\IEDDefine\WTJND.xml"
)

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
    protocol_defaults = defaults_for_xml(XML_PATH.stem)
    check(len(device.groups) == 10, f"组数 = {len(device.groups)}")
    counts = device.point_counts()
    check(
        counts["YC"] == 4 and counts["YX"] == 19 and counts["YK"] == 5,
        "遥测 4 / 遥信 19 / 遥控 5",
    )
    check(
        counts["FIX"] in (0, 3),
        f"定值点数 = {counts['FIX']}（合并表为 3）",
    )

    group = device.find(0x1B, 0x01)
    check(group is not None, "存在 0x1B/01 定值组")
    assert group is not None
    check(not group.read_seen, "定值组默认未读（等待 0x1B 请求）")
    apply_group_defaults(group, protocol_defaults)
    check(len(group.data) == 8, f"0x1B/01 数据区长度 = {len(group.data)}")
    check(
        format_hex(bytes(group.data)) == "00 18 12 5C 00 00 00 64",
        "协议默认值为 24AH / 47.00V / 不报警 / 100%",
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
        fix_defaults = defaults_for_xml(FIX_XML_PATH.stem)
        apply_group_defaults(fix_group, fix_defaults)
        check(
            len(fix_group.data) == 8,
            "协议默认值补齐为 8 字节（含剩余容量）",
        )
        fix_engine = ProtocolEngine(fix_device)
        response = fix_engine.handle(request)
        assert response is not None
        check(
            format_hex(response)
            == format_hex(
                FrameCodec.build(
                    1, 0x1B, 0x01, parse_hex("00 18 12 5C 00 00 00 64")
                )
            ),
            "WTJND.xml 回出协议默认 0x1B01",
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

    print("== 组键解析 ==")
    check(DeviceModel.parse_group_key("27/1") == (0x1B, 0x01), "27/1 按十进制解析")
    check(
        DeviceModel.parse_group_key("1B/01") == (0x1B, 0x01),
        "1B/01 按十六进制解析",
    )
    check(
        DeviceModel.parse_group_key("0x1B/0x01") == (0x1B, 0x01),
        "0x1B/0x01 按十六进制解析",
    )

    print("== 预设 ==")
    preset = collect_preset(device)
    check("0x0101" in preset["values"], "预设包含 0x0101")
    preset_group = device.find(0x01, 0x01)
    assert preset_group is not None
    original = bytes(preset_group.data)
    preset_group.data[0] ^= 0xFF
    applied, skipped, notes = apply_preset(device, preset)
    check(applied > 0 and skipped == 0 and not notes, f"预设应用 {applied} 组")
    check(bytes(preset_group.data) == original, "预设恢复 0x0101 数据区")
    with tempfile.TemporaryDirectory() as temp_dir:
        preset_path = Path(temp_dir) / "preset.json"
        save_preset(preset_path, device)
        loaded = load_preset(preset_path)
        check(
            loaded["values"].get("0x0101") == preset["values"].get("0x0101"),
            "预设保存/加载",
        )

    print("== 点格式 ==")
    fmt = PointFormat("u16be")
    check(fmt.decode_raw(parse_hex("12 5C")) == 4700, "u16be 解码")
    check(fmt.encode_raw(4700, 2) == parse_hex("12 5C"), "u16be 编码")

    fmt = PointFormat("i16be")
    check(fmt.decode_raw(parse_hex("FF D8")) == -40, "i16be 解码负数")
    check(fmt.encode_raw(-40, 2) == parse_hex("FF D8"), "i16be 编码负数")

    fmt = PointFormat("u16be", offset=-40.0, scale=1.0)
    check(fmt.decode_eng(parse_hex("00 00"), 1.0) == -40, "偏移解码")
    check(fmt.encode_eng(0.0, 2, 1.0) == parse_hex("00 28"), "偏移编码")

    fmt = PointFormat("f32be")
    float_data = struct.pack(">f", 47.5)
    check(abs(fmt.decode_eng(float_data, 1.0) - 47.5) < 1e-5, "float32 解码")
    check(fmt.encode_eng(47.5, 4, 1.0) == float_data, "float32 编码")

    fmt = PointFormat("auto")
    check(fmt.decode_raw(parse_hex("12 5C")) == 4700, "auto 2 字节")
    check(
        fmt.decode_raw(parse_hex("00 00 00 00 00 00 00 64")) == 100,
        "auto 8 字节按原始大端",
    )

    point = Point("YC", "温度", 1, 17, 0, 2)
    key = point_key(point)
    check(key == "1/17/0/2/YC", f"点键 = {key}")
    with tempfile.TemporaryDirectory() as temp_dir:
        format_path = Path(temp_dir) / "point_formats.json"
        save_formats(format_path, "DEMO", {key: PointFormat("i16be")})
        loaded = load_formats(format_path, "DEMO")
        check(loaded[key].code == "i16be", "点格式保存/加载")
        other = Point("YC", "浮点", 1, 3, 0, 4)
        save_formats(
            format_path,
            "OTHER",
            {point_key(other): PointFormat("f32be")},
        )
        check(
            load_formats(format_path, "DEMO")[key].code == "i16be"
            and load_formats(format_path, "OTHER")[
                point_key(other)
            ].code
            == "f32be",
            "不同 XML 的点格式互不覆盖",
        )

    print("== 电池模型 ==")
    model = BatteryModel()
    check(
        abs(model.soc - 100.0) < 1e-6 and abs(model.voltage - 53.5) < 1e-6,
        "初始 100% / 53.5V",
    )
    model.ac_present = False
    model.tick(3600)
    check(58.0 < model.soc < 59.0, f"放电 1 小时 SOC={model.soc:.2f}%")
    check(not model.snapshot()["undervoltage"], "1 小时未欠压")
    model.tick(3600)
    check(model.snapshot()["undervoltage"], "2 小时后欠压")
    model.reset()
    model.phase_loss = [True, False, False]
    snapshot = model.snapshot()
    check(snapshot["phase_a_loss"] and not snapshot["ac_loss"], "单相失电不报交流失电")
    model.phase_loss = [True, True, False]
    check(model.snapshot()["ac_loss"], "两相失电报交流失电")
    model.reset()
    model.start_activation()
    model.tick(600)
    snapshot = model.snapshot()
    check(
        snapshot["activation"] and 9.0 < model.max_discharge_minutes < 11.0,
        "活化放电 10 分钟",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "battery_config.json"
        yc_group = device.find(1, 1)
        assert yc_group is not None and yc_group.points
        point_value_key = point_key(yc_group.points[0])
        custom = BatteryConfig(point_values={point_value_key: 48.0})
        save_battery_config(config_path, custom)
        loaded = load_battery_config(config_path)
        check(
            loaded.point_values.get(point_value_key) == 48.0,
            "电池参数只保存点值配置",
        )
        check(
            loaded.capacity_ah == 24.0,
            "旧模型参数不写入配置文件",
        )
        config_path.write_text("not json", encoding="utf-8")
        check(
            load_battery_config(config_path).capacity_ah == 24.0,
            "坏参数文件回退默认",
        )

    yc_group = device.find(1, 1)
    assert yc_group is not None and yc_group.points
    point_value_key = point_key(yc_group.points[0])
    updated, notes = apply_point_values(
        device, {point_value_key: 48.0}, {}, yc_group.key
    )
    raw_value = int.from_bytes(
        yc_group.data[0:2], "big"
    )
    check(
        updated == 1 and not notes and raw_value == 48000,
        "配置点值按工程值写入遥测",
    )

    check(
        role_for_point(Point("YC", "电池电压", 1, 1, 0, 2))
        == "battery_voltage",
        "电池电压映射",
    )
    check(
        role_for_point(Point("YX", "A 相失电告警信号", 1, 2, 0, 0))
        == "phase_a_loss",
        "A 相失电映射",
    )
    check(
        role_for_point(Point("YX", "操作电压欠压告警信号", 1, 4, 1, 0))
        is None,
        "操作电压欠压告警不误判为遥测",
    )

    model.reset()
    model.ac_present = False
    model.tick(60)
    updated, notes = apply_snapshot(device, model.snapshot())
    check(updated > 0 and not notes, f"模型写入 {updated} 个点")
    data_0104 = bytes(device.find(1, 4).data)
    check((data_0104[1] & 0x05) == 0x05, "交流失电/后备投入位写入 0x0104")

    print("== XML 校验 ==")
    bad_xml = """<?xml version="1.0" encoding="GB2312"?>
<IEDDefine>
<MeasureDefine Count="3">
<MeasureDefine0 GroupNo="1" EntryNo="1" FUN="0" INF="2" Name="A" Factor="1" Enable="1"/>
<MeasureDefine1 GroupNo="1" EntryNo="1" FUN="1" INF="2" Name="B重叠" Factor="1" Enable="1"/>
<MeasureDefine2 GroupNo="1" EntryNo="1" FUN="300" INF="2" Name="C越界" Factor="1" Enable="1"/>
</MeasureDefine>
<SignalDefine Count="3">
<SignalDefine0 GroupNo="1" EntryNo="2" FUN="0" INF="0" Name="X" Enable="1"/>
<SignalDefine1 GroupNo="1" EntryNo="2" FUN="0" INF="0" Name="X重复" Enable="1"/>
<SignalDefine2 GroupNo="1" EntryNo="2" FUN="0" INF="8" Name="Y坏位" Enable="1"/>
</SignalDefine>
</IEDDefine>
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        bad_path = Path(temp_dir) / "bad.xml"
        bad_path.write_text(bad_xml, encoding="gb18030")
        bad_device = DeviceModel()
        bad_device.load_xml(bad_path)
        issues = validate_device(bad_device)
        messages = [str(issue) for issue in issues]
        check(len(messages) >= 4, f"发现问题 {len(messages)} 条")
        check(any("数据区重叠" in m for m in messages), "识别数据区重叠")
        check(any("同一字节/位" in m for m in messages), "识别遥信重复位")
        check(any("不在 0..7" in m for m in messages), "识别遥信位号越界")
        check(any("FUN=300" in m for m in messages), "识别 FUN 越界")

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
        multi_device.find(0x1B, 0x01).data = bytearray(
            parse_hex("00 18 12 5C")
        )
        multi_device.find(0x1B, 0x02).data = bytearray(
            parse_hex("00 5A 08 66")
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