"""故障注入：在不改协议逻辑的前提下，对生成的响应做可控破坏。

用于验证 DTU 的：
- 重发/超时/通讯中断
- 校验和错误处理
- 地址过滤
- 短帧/粘包恢复
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FaultConfig:
    drop_all: bool = False
    delay_ms: int = 0
    bad_checksum: bool = False
    wrong_address: bool = False
    short_frame: bool = False
    drop_every_n: int = 0


@dataclass
class FaultResult:
    data: bytes | None
    delay_ms: int
    notes: list[str] = field(default_factory=list)


class FaultInjector:
    """按帧计数的轻量故障注入器。"""

    def __init__(self) -> None:
        self.count = 0

    def reset(self) -> None:
        self.count = 0

    def apply(self, response: bytes, config: FaultConfig, addr: int) -> FaultResult:
        self.count += 1
        notes: list[str] = []

        if config.drop_all:
            return FaultResult(None, 0, ["不回帧"])
        if config.drop_every_n > 0 and self.count % config.drop_every_n == 0:
            return FaultResult(
                None,
                0,
                [f"每 {config.drop_every_n} 帧丢 1 帧（第 {self.count} 帧）"],
            )

        data = bytearray(response)
        if len(data) < 7:
            return FaultResult(bytes(data), 0, ["响应过短，未注入"])

        if config.wrong_address:
            new_addr = (addr % 247) + 1
            data[3] = new_addr
            data[-1] = sum(data[:-1]) & 0xFF
            notes.append(f"错地址 -> {new_addr}")

        if config.bad_checksum:
            data[-1] ^= 0xFF
            notes.append("错校验")

        if config.short_frame:
            data = data[:-1]
            notes.append("短帧（少 1 字节）")

        if config.delay_ms > 0:
            notes.append(f"延迟 {config.delay_ms} ms")

        return FaultResult(bytes(data), max(0, config.delay_ms), notes)