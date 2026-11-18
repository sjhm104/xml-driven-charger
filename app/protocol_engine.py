"""通用 EB90 协议引擎（版本 2 / 通用充电机语义）。

响应规则：
    0x01 查询   -> 0x81，子类型不变，数据区取对应组缓冲区
    0x02 控制   -> 0x82，1 字节结果（0x01 成功 / 0x81 失败）
    0x1A 写定值 -> 0x1A，回显组数据区（DTU 会逐点比较）
    0x1B 读定值 -> 0x1B，数据区取对应组缓冲区
    0x83 均浮充 -> 0x83，1 字节结果（兼容南瑞版 0x03 请求）
    0x1A/0x0C 恢复出厂 -> 0x1A，1 字节结果
"""

from __future__ import annotations

from .device_model import DeviceModel
from .frame_codec import Frame, FrameCodec


class ProtocolEngine:
    def __init__(self, device: DeviceModel) -> None:
        self.device = device
        self.default_result = 0x01
        self.last_note = ""

    def handle(self, frame: Frame) -> bytes | None:
        self.last_note = ""
        func = frame.func
        subtype = frame.subtype
        addr = frame.addr

        if func == 0x01:
            group = self.device.find(0x01, subtype)
            if group is None:
                self.last_note = f"未定义查询组 0x01/{subtype:02X}"
                return None
            return FrameCodec.build(addr, 0x81, subtype, bytes(group.data))

        if func == 0x02:
            group = self.device.find(0x02, subtype)
            result = group.result if group is not None else 0x81
            if group is None:
                self.last_note = f"未定义控制组 0x02/{subtype:02X}，按失败应答"
            return FrameCodec.build(addr, 0x82, subtype, bytes((result,)))

        if func == 0x1A:
            # 恢复出厂设置：Subtype=0x0C，数据区 FF FF
            if subtype == 0x0C:
                self.last_note = "恢复出厂设置"
                return FrameCodec.build(
                    addr, 0x1A, subtype, bytes((self.default_result,))
                )

            # 写定值：XML 里定值组通常用 GroupNo=0x1B
            group = self.device.find(0x1B, subtype)
            if group is None:
                self.last_note = f"未定义定值组 0x1B/{subtype:02X}，原样回显"
                return FrameCodec.build(addr, 0x1A, subtype, frame.data)

            for index, value in enumerate(frame.data):
                if index < len(group.data):
                    group.data[index] = value
                else:
                    group.data.append(value)
            self.last_note = (
                f"写入定值 0x1B/{subtype:02X}，回显 {len(group.data)} 字节"
            )
            return FrameCodec.build(addr, 0x1A, subtype, bytes(group.data))

        if func == 0x1B:
            group = self.device.find(0x1B, subtype)
            if group is None:
                self.last_note = f"未定义定值组 0x1B/{subtype:02X}"
                return None
            return FrameCodec.build(addr, 0x1B, subtype, bytes(group.data))

        # 0x83 标准版；0x03 南瑞版请求，统一回 0x83
        if func in (0x83, 0x03):
            self.last_note = "均浮充间隔设置"
            return FrameCodec.build(
                addr, 0x83, subtype, bytes((self.default_result,))
            )

        return None