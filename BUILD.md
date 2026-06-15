# 打包成桌面 App 并分享(P3)

把模拟盘打成一个**自包含的本地 app**,压缩包发给同事,同事解压双击即用,数据各自留在本机。

## 构建(在你的机器上)

```bash
.venv/bin/python -m pip install pyinstaller   # 首次
./build_app.sh
```

产物:
- `dist/PaperTrading/` —— 可执行目录(里面的 `PaperTrading` 是入口)
- `dist/PaperTrading-v<版本>-<os>-<arch>.zip` —— **发给同事的压缩包**(精简版约 11MB)

## 同事怎么用

1. 解压 zip
2. 运行里面的 `PaperTrading`
   - macOS:首次可能被 Gatekeeper 拦,右键 → 打开,或 `xattr -dr com.apple.quarantine PaperTrading/`
3. 等几秒(首次启动 ~8 秒),浏览器自动打开模拟盘
4. 关闭那个控制台窗口即退出

每位同事的数据自动落到各自的用户目录,互不干扰:
- macOS: `~/Library/Application Support/PaperTrading/`
- Windows: `%APPDATA%\PaperTrading\`
- Linux: `~/.local/share/PaperTrading/`

## 关于数据源:完整版 vs 精简版

`paper_trading.spec` 支持两种构建,**默认完整版**:

- **完整版(默认)**:把真实数据源依赖(`mootdx / pandas / numpy / rqdatac / pymysql`)一起打进去,桌面 app 里**通达信 / 米筐 / Wind 都能用**(米筐/Wind 仍需在数据源页填 license / VPN 凭证)。包较大(macOS 约 117MB)。前提:这些依赖已装进 `.venv`(`requirements.txt` 已列);spec 用 `find_spec` 探测,**装了才打进去,没装就自动跳过**。
  ```bash
  .venv/bin/pip install -r requirements.txt   # 首次,装齐数据源依赖
  ./build_app.sh
  ```
- **精简版**:只内置 `fixture`(合成行情),体积小(约 28MB),适合只需演示模拟交易/回测/绩效/agent 的同事。构建时设环境变量:
  ```bash
  PT_LEAN=1 ./build_app.sh
  ```

> 注:用真实数据源的策略若依赖 pandas 等,也只有完整版能跑;精简版里这类策略会在 worker 子进程报缺依赖。
> GitHub Actions 出的 Release 包是**完整版**(CI 会装好数据源依赖;`rqdatac` 在个别 runner 装不上时该平台的包不含米筐,其余数据源不受影响)。

## 更新策略(不丢数据)

代码与数据是分离的(数据在用户目录,由 `PAPER_TRADING_HOME` 决定):
- 重新构建、发新 zip,同事**替换** app 目录即可,**数据不动**。
- 破坏性的数据结构变更才需要迁移;`api_version`(见 `/api/meta`)用于兼容判断。

## 跨平台:出 Windows 版

PyInstaller **不能跨平台交叉编译**——Mac 上只能打 Mac 包,Windows 上只能打 Windows 包。代码本身已跨平台(`paths.py` 处理 `%APPDATA%`、pywebview 在 Windows 自动用 WebView2、`paper_trading.spec` 按平台自动选 `.ico`/`.icns` 并只在 macOS 生成 `.app`),所以**只差在 Windows 上跑一次构建**。

在一台 Windows 电脑上:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller
build_app.bat
```

产出 `dist\PaperTrading\PaperTrading.exe` 和 `dist\PaperTrading-v<版本>-windows.zip`。同事解压双击 `PaperTrading.exe` 即用(首次弹出原生窗口;Windows 10/11 自带 WebView2)。

**没有 Windows 机器?** 用 GitHub Actions 云端构建(代码推到 GitHub,加一个 windows-runner 的 workflow 自动出 `.exe`)。需要的话让我配置 `.github/workflows/build.yml`。

> 注:Apple 芯片 Mac 上的 Windows 虚拟机是 ARM 版,产出的 exe 多数 x86 PC 跑不了,不推荐。

## agent 工具也在包里

`agent/`(SDK / CLI / SKILL.md)随包分发。同事(或其 agent)可在 app 运行时用:
```bash
python3 <解压目录>/agent/cli.py meta --base-url http://127.0.0.1:<端口>
```
(端口在启动时控制台会打印。)
