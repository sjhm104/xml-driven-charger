"""EB90 帧编解码。

帧格式：
    EB 90 | 长度(1) | 地址(1) | 功能码(1) | 子类型(1) | 数据区(N) | 校验和(1)

长度 = 地址 + 功能码 + 子类型 + 数据区，即 3 + N。
校验和 = 从 EB 到数据区最后一个字节的 8 位累加和。
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER = b"\xEB\x90"
MIN_BODY_LEN = 3  # 地址 + 功能码 + 子类型
MAX_BODY_LEN = 255
MAX_FRAME_LEN = MAX_BODY_LEN + 4


@dataclass
class Frame:
    raw: bytes
    addr: int
    func: int
    subtype: int
    data: bytes
    valid: bool = True
    error: str = ""

    @property
    def fun_type(self) -> int:
        """(功能码 << 8) | 子类型，对应装置类型表的 组号/条目号。"""
        return (self.func << 8) | self.subtype

    def hex(self) -> str:
        return self.raw.hex(" ").upper()

    def summary(self) -> str:
        return (
            f"addr={self.addr} func=0x{self.func:02X} "
            f"sub=0x{self.subtype:02X} data={len(self.data)}B | {self.hex()}"
        )


class FrameCodec:
    """无状态编解码器，可被 UI 和测试共用。"""

    @staticmethod
    def checksum(data: bytes) -> int:
        return sum(data) & 0xFF

    @staticmethod
    def build(addr: int, func: int, subtype: int, data: bytes = b"") -> bytes:
        body = bytes((addr & 0xFF, func & 0xFF, subtype & 0xFF)) + bytes(data)
        if len(body) > MAX_BODY_LEN:
            raise ValueError("报文数据区过长")
        head = HEADER + bytes((len(body),)) + body
        return head + bytes((sum(head) & 0xFF,))

    @staticmethod
    def extract(buf: bytearray) -> list[Frame]:
        """从缓冲区中提取完整帧。

        会就地删除已消费的字节；不完整帧留在缓冲区中等待下一次数据。
        """
        frames: list[Frame] = []
        while True:
            idx = buf.find(HEADER)
            if idx < 0:
                # 没有帧头。保留最后 1 字节，防止 0xEB 被截断。
                if len(buf) > 1:
                    del buf[:-1]
                break
            if idx:
                del buf[:idx]
            if len(buf) < 4:
                break

            body_len = buf[2]
            total = body_len + 4
            if body_len < MIN_BODY_LEN or total > MAX_FRAME_LEN:
                frames.append(
                    Frame(bytes(buf[:1]), 0, 0, 0, b"", False, f"长度非法: {body_len}")
                )
                del buf[0]
                continue
            if len(buf) < total:
                break

            raw = bytes(buf[:total])
            if (sum(raw[:-1]) & 0xFF) != raw[-1]:
                frames.append(
                    Frame(raw, raw[3], raw[4], raw[5], raw[6:-1], False, "校验和错误")
                )
                # 只丢 1 字节，继续向后找下一个 EB 90
                del buf[0]
                continue

            frames.append(Frame(raw, raw[3], raw[4], raw[5], raw[6:-1], True))
            del buf[:total]
        return frames