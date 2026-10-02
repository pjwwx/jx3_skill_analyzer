# 剑网3副本解析器 3.1.2

这是旧版 `ngb.py` 的重做版。程序按所选副本及难度，从本机 PakV4 读取技能、Buff、NPC、交互物品、地图对象和官方资料，关联配置与 UI 文案，并静态分析普通 Lua 源码和标准 Lua 字节码。

本仓库只提供程序源码、构建脚本和文档，不提交测试目录、预编译 EXE、打包产物、游戏数据或解析结果。

## 使用

1. 在本机完成构建后，双击 `JX3SkillAnalyzer.exe`。
2. 选择正式服或体服的 `bin64` 目录，点击“读取 / 刷新副本列表”。
3. 搜索并选择一个或多个副本及难度，也可以通过地图 ID 选择。
4. 选择结果存放位置，开始解析。每次自动新建 `技能解析_日期_时间` 目录，同一秒重复运行会追加序号，保留旧结果。

界面默认以普通窗口打开，按屏幕大小调整尺寸；较小屏幕可滚动操作。完成、警告和错误提示使用统一浅色背景和深色文字，在暗色系统主题下也能阅读。

自动模式根据副本引用读取 V4 资料，不需要手工提供内部路径清单。共享技能目录使用同一技能集合，NPC、地图对象和官方资料按所选地图区分。NPC、AI 分表先建立表头索引，再读取所选副本关联的正文。解包组件从程序自己的临时目录运行，加载所选游戏目录中的 V4 DLL，结果按内部目录保存。

“使用已解包文件（兼容）”页保留原有流程，可手工选择解包根目录与技能 ID 文本。`示例技能ID.txt` 可用于此模式。

## 结果

每次运行目录只有三个入口：

- `副本解析.xlsx`：概览、技能、关联 Buff、NPC、交互物品、地图对象、官方百科、官方 DBM 和问题分工作表保存。没有资料的表不生成空页。
- `JSON/`：完整字段、解析事实、来源证据、逐文件读取记录和诊断。技能 ID 文本与副本选择记录保存在解析结果的 `run_inputs` 中。
- `原始资料/`：本次解出的原文件、静态反编译文件和独立恢复函数，保留内部目录。

主要资料表采用 24 磅紧凑行高、11 磅统一字体和浅色交替底纹，常用字段优先显示，表头可筛选，名称列冻结。技能与 Buff 名称优先采用 UI 名称，缺失时使用配置名称。原始名称、帧数、脚本和来源字段折叠在右侧，可点击列上方的加号展开。长文字可在公式栏查看或手动增加行高；完整原值也保存在 JSON 中。

官方 DBM 的规则与提示按原文件、原行关联，保留控制行。`BossDbmMap<地图ID>.tab` 与副本名称、难度关联；读取不到时会在“问题”中列出具体地图与路径。可选小地图索引未采用和副本 DBM 缺失分别说明。

错误、警告和资料缺失统一放在“问题”中，重复依赖问题合并并记录受影响技能。`doodad` 统一称为“交互物品”。

## 解析方式与边界

- 根据文件头识别 Lua 字节码，先离线反编译，再依据字段赋值和调用签名分析；不依赖被删除的局部变量名，也不执行游戏 Lua。
- 追踪本地 Include 和脚本引用，只分析主脚本调用到的共享函数，避免把其他技能效果串入当前技能。
- 反编译器固定为 `unluac-rs 1.4.4`。全文存在恢复诊断时，不把诊断伪代码作为可靠逻辑。
- 对 `scripts/Include/Skill.lh`，只将原字节码能够明确定位、没有外层捕获的命名函数独立严格反编译。完整文件、表初始化和未恢复函数继续保留诊断与限制。
- 兼容反编译字符串中的中文路径字节转义，保留无法静态确定的表达式。数值表达式不使用 Python `eval`。
- `SkillPuncture` 为穿刺伤害，`SkillPenetration` 为穿透，两列分别记录。
- NPC 的 `MaxLife`、`MaxLifeCount` 保留配置原值，不能直接相乘推定实战血量。空值继承仅作为候选信息。
- 尸体交互物品依据 `CorpseDoodadID` 关联，不由此推定掉落或完整运行时交互过程。
- 地图预置、模板、百科和 DBM 是不同证据，不代表完整运行时刷怪。百科技能文案 ID 不是实际技能 ID，展示 NPC 也可能来自其他难度。
- 客户端缺失的服务器端依赖无法恢复。静态结果记录本地可见的配置和逻辑，不代表运行时每个分支都会触发。

## 命令行

列出副本、难度与地图 ID：

```powershell
py -3.10 main.py --no-gui --bin64 "D:\JX3\bin\zhcn_exp\bin64" --list-dungeons
```

按地图 ID 分析：

```powershell
py -3.10 main.py --no-gui --bin64 "D:\JX3\bin\zhcn_exp\bin64" --map-id 836 --output "解析结果"
```

也可用 `--dungeon "一之窟"` 按名称选择；`--dungeon` 和 `--map-id` 都可以重复指定。

兼容已解包资料：

```powershell
py -3.10 main.py --no-gui --root "D:\全量版.extract" --ids "示例技能ID.txt" --output "解析结果"
```

可用 `--skills-tab`、`--buff-tab`、`--skill-ui`、`--buff-ui` 和 `--decompiler` 覆盖自动路径。直接运行源码前，需要安装 `requirements-build.txt` 中的依赖；自动 V4 模式还需要先构建原生桥组件。

## 从源码构建

完整打包需要 Windows x64、Git for Windows、Python 3.10、Visual Studio C++ x64 工具集 14.29 和 Windows SDK，以及 Rust/Cargo 1.94 或更高版本。

原生桥静态链接 VC++ 运行库。为兼容当前游戏 DLL 的 V4 初始化，`build-native.ps1` 固定使用无优化的 Debug CRT 参数（`/Od /MTd /D_DEBUG`）。`build-unluac.ps1` 从官方仓库的 `v1.4.4` 标签构建反编译器。

可以分别构建组件：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-native.ps1
powershell -ExecutionPolicy Bypass -File .\build-unluac.ps1
```

也可以运行总构建脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

总构建脚本会更新缺失或落后的组件、创建隔离的 `.venv`、安装依赖，再使用 PyInstaller 打包。原生源码比桥组件更新时会重新编译；现有反编译器版本不是 1.4.4 时会重新构建。

完成后 EXE 位于 `dist/JX3SkillAnalyzer.exe`，运行程序无需安装 Python、Java 或 Lua。程序使用系统临时目录，构建产生的 `.venv/`、`.deps/`、`build/`、`dist/`、`.spec` 和二进制文件均由 Git 忽略。

## 第三方组件

PakV4 互操作部分基于 [jx3pak/PakV4-Extract](https://github.com/jx3pak/PakV4-Extract) 的公开源码，使用了游戏 DLL 接口、V4 初始化和资源读取流程；本项目重写了命令行、路径与输出管理、诊断和 GUI 调用。

字节码反编译使用 [unluac-rs](https://github.com/x3zvawq/unluac-rs) 1.4.4，桌面界面使用 PySide6 Essentials，Excel 导出使用 openpyxl。完整来源与许可见 `THIRD_PARTY_NOTICES.md` 和 `vendor/LICENSE-unluac-rs.txt`。
