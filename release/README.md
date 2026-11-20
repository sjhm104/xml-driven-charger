# 发布包

| 文件 | 说明 |
|---|---|
| `ChargerSim_v0.1.zip` | 充电机模拟器 v0.1，Windows 64 位，解压即用，无需安装 Python |
| `ChargerSim_v0.1.zip.sha256` | 发布包 SHA256 校验值 |

## 使用

1. 下载并解压 `ChargerSim_v0.1.zip`。
2. 进入 `ChargerSim` 文件夹，双击 `ChargerSim.exe`。
3. 详细接线、界面和排查说明见压缩包内 `README.md`，或仓库根目录的 [README.md](../README.md)。

## 校验

```bat
certutil -hashfile ChargerSim_v0.1.zip SHA256
```

结果应与 `ChargerSim_v0.1.zip.sha256` 中的值一致：

```
e73ac3dfefc6e7701bf5cbc6a57c6b8e8ed67716873f5930f4b6724e6b3ac942
```

## 说明

- 本目录只存放打包后的发布产物。
- 源码位于 `app/`，重新打包执行 `build_exe.bat`。
- 工具不会修改装置类型表 XML。