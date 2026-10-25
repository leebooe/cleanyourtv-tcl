# TCL：通过多屏互动安装应用预览版

预览版以普通应用显示在电视的应用列表中，包名为 `com.gathin.tvhome.preview`，
简体中文名称为「主屏幕」（英文为 Home，随系统语言变化）。默认桌面、遥控器 Home 行为和现有无障碍服务保持原配置。
原版 `assembleRelease` 继续生成包名为 `com.gathin.tvhome` 的 HOME 桌面。

## 构建

使用 JDK 17、Android SDK Platform 34 和 Build Tools 34.0.0，从源码编译：

```bash
cd launcher
./gradlew --no-daemon testPreviewUnitTest lintPreview assemblePreview
cd ..
```

产物：`launcher/app/build/outputs/apk/preview/app-preview.apk`。
预览构建沿用 release 的 R8 优化和资源收缩，使用独立包名，可与原版共存。
其应用入口声明 `LAUNCHER` 与 `LEANBACK_LAUNCHER`，作为手动打开的应用使用。

## 安装

电脑和电视连接同一局域网。电视开启「多屏互动」，在 ADB 已授权的情况下执行：

```bash
export ANDROID_HOME=/path/to/android-sdk
python3 tools/tcl-install.py --tv 192.168.1.50
```

安装工具检查 APK 的包名与 HOME 声明，使用 SDK 中的 `aapt` 读取版本。
`--aapt /path/to/aapt` 和 `--adb /path/to/adb` 可显式指定工具。
工具在电脑 loopback 启动临时 HTTP 服务，只提供指定 APK，再通过自动分配端口的
`adb reverse` 供电视下载。T-Cast 使用 TCP 6553，并在等待期间发送心跳。
下载结束、拒绝、失败、超时或中断后，工具清理本次反向端口和 HTTP 服务。

电视弹出安装提示后，用遥控器选择「同意」。工具只执行安装，随后可从现有桌面的
「应用」页面打开「主屏幕」。按 Home 按钮回到电视原有桌面。

部分固件只接受公网下载地址。可将自己编译的同一 APK 放到自己的 HTTP(S) 地址，再执行：

```bash
python3 tools/tcl-install.py --tv 192.168.1.50 \
  --url https://your-host.example/CleanYourTV.apk
```

此路径通过多屏互动直接下载，使用本地 APK 提取的包名与版本信息。
电视拒绝安装时，工具停止并报告失败。ADB 调试的关闭通过电视设置按家庭现有使用安排处理。

协议实现参考本机 `aerial-tv/tools/tcast-push.py` 与 `aerial-tv/docs/tcast-protocol.md`。
实机验证记录保存到本项目忽略的 `work/`、`backup/` 和 `shots/`。
