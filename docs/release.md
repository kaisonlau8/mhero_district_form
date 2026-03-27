# 打包与发版文档

## 1. 文档目标

本文档面向需要负责以下工作的维护人员：

- 构建 macOS 桌面应用
- 生成 zip 安装包
- 发布 GitHub Release
- 做版本更新和回归检查

## 2. 当前发版方式概览

当前项目采用“本地可打包 + GitHub Actions 可自动上传 Release”双通道模式：

### 2.1 本地通道

适合：

- 先在本机验证桌面版是否可用
- 先拿到 `.app` 和 zip 再手动上传
- GitHub Actions 临时失败时的兜底方案

### 2.2 自动通道

适合：

- 正式版本发版
- 希望把 tag 与 release 绑定
- 希望仓库自动保存发布产物

触发条件：

- 推送 `v*` 标签，例如 `v0.1.2`

## 3. 打包前准备

### 3.1 环境要求

- macOS
- Python 3.12
- 可正常安装依赖
- 本地能运行 PyInstaller

### 3.2 依赖安装

```bash
cd /Users/i/myCode/报表项目
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pyinstaller
```

### 3.3 打包前检查项

打包前建议先确认：

1. `git status` 是干净的，或者你明确知道有哪些未提交变更
2. `app/assets/report_template.xlsx` 是当前最新默认模板
3. 网页模式已经验证通过
4. 桌面模式已经验证通过
5. 如果修改了打包文件名或 release 规则，`.github/workflows/release.yml` 也同步更新了

## 4. 本地打包

### 4.1 执行打包

```bash
cd /Users/i/myCode/报表项目
source .venv/bin/activate
python build_macos_app.py
```

### 4.2 打包脚本做了什么

`build_macos_app.py` 会自动执行以下动作：

1. 删除旧的 `build/`
2. 删除旧的 `dist/`
3. 删除旧的 `.spec`
4. 调用 PyInstaller 打包 `desktop_app.py`
5. 将 `app/static` 和 `app/assets` 打进应用
6. 生成 `区域报表自动生成.app`
7. 生成 `mhero-district-form-macos-arm64.zip`

### 4.3 打包产物

打包完成后会得到：

- `dist/区域报表自动生成.app`
- `dist/mhero-district-form-macos-arm64.zip`

说明：

- `.app` 适合本机直接测试
- `.zip` 适合上传到 GitHub Release

## 5. 本地打包后验证

建议至少执行以下 smoke test：

### 5.1 应用可启动

双击：

- `dist/区域报表自动生成.app`

确认：

- 可以正常打开窗口
- 页面能加载，不是白屏

### 5.2 导入可用

确认：

- 7 份文件可以识别
- 模板可以替换
- 按钮能在识别成功后变为可点击

### 5.3 保存可用

确认：

- 点击生成后会弹系统保存对话框
- 选择路径后能保存成功
- 保存出的文件可被 Excel 打开

### 5.4 结果正确

确认：

- 输出文件名格式正确
- `续保` 拼接正确
- `门店备件库存导出` 的 `M` 列在模板中能参与计算
- `总表` 打开后能自动重算

## 6. GitHub Actions 自动发版

### 6.1 工作流文件

位置：

- `.github/workflows/release.yml`

### 6.2 触发方式

以下两种情况都可触发：

- 推送 `v*` tag
- 在 GitHub Actions 页面手动运行 `workflow_dispatch`

### 6.3 自动发版流程

工作流会：

1. 拉取代码
2. 安装 Python 3.12
3. 安装项目依赖和 PyInstaller
4. 运行 `python build_macos_app.py`
5. 将 `dist/mhero-district-form-macos-arm64.zip` 上传到对应 Release

## 7. 推荐发版流程

建议使用下面的固定步骤：

### 7.1 完成功能修改

先完成功能开发与本地验证。

### 7.2 提交代码

```bash
git status
git add .
git commit -m "你的发版说明"
git push origin main
```

### 7.3 创建版本标签

```bash
git tag v0.1.2
git push origin v0.1.2
```

### 7.4 等待 GitHub Actions 完成

到仓库的 Actions 页面确认：

- 工作流成功
- Release 已创建或已更新
- 资产已上传

### 7.5 下载验证 Release 资产

建议从 GitHub Release 页面下载最终 zip，重新解压并试跑一次。

这样可以确认“用户真正下载到的产物”是正确的，而不是只验证本地 `dist/` 目录。

## 8. 版本命名建议

当前项目没有单独的版本文件，版本以 Git tag 为准。

建议采用语义化版本风格：

- `v0.1.0`：首个可用版本
- `v0.1.1`：小修复
- `v0.2.0`：新增明显功能但不破坏整体结构
- `v1.0.0`：业务和交付方式稳定后的正式版

建议规则：

- 修 bug：升 patch
- 新增功能：升 minor
- 大改架构或不兼容变化：升 major

## 9. Release 文案建议

每次发版时建议在 Release 说明里至少写清楚：

- 这次修了什么
- 这次新增了什么
- 适用平台
- 是否未签名
- 用户如果打不开该怎么做

建议模板：

```text
本次更新：
- 修复 xxx
- 新增 xxx
- 调整 xxx

说明：
- 当前为未签名应用
- 适用平台：macOS Apple Silicon (arm64)
- 如首次打开被拦截，请在 Finder 中右键应用后选择“打开”
```

## 10. 常见发版问题

### 10.1 GitHub Release 资产名异常

历史上出现过中文 zip 文件名在 GitHub Release 中展示异常的问题。

因此当前约定使用 ASCII 文件名：

- `mhero-district-form-macos-arm64.zip`

如果未来你改动了压缩包命名，请同步检查：

- `build_macos_app.py`
- `.github/workflows/release.yml`
- README 与发版说明

### 10.2 桌面版导出时报路径错误

历史上出现过：

- `[Errno 17] File exists: '/'`

原因是 macOS 保存对话框返回的是字符串路径，而旧代码误按列表取值，导致路径被截断成 `/`。

如果未来再次出现保存相关问题，请优先检查：

- `desktop_app.py`
- `DesktopBridge.save_report`

### 10.3 Actions 成功但 Release 没有正确资产

建议按以下顺序排查：

1. 看 workflow 日志里 `Build macOS app` 是否成功
2. 检查 `dist/` 目录里是否确实生成了 zip
3. 检查 workflow 中上传路径是否与构建产物一致
4. 检查 tag 对应的 Release 是否已存在并被正确匹配

### 10.4 用户打不开应用

当前是未签名应用，这在内部工具分发中是预期行为。

可引导用户：

1. 在 Finder 中找到应用
2. 右键应用
3. 点击“打开”
4. 系统确认后再次打开

## 11. 手动兜底发版方案

如果 GitHub Actions 失败，但本地已成功打包：

1. 本地执行 `python build_macos_app.py`
2. 拿到：
   - `dist/区域报表自动生成.app`
   - `dist/mhero-district-form-macos-arm64.zip`
3. 在 GitHub 上手动创建 Release
4. 上传 zip 资产
5. 补充 release 说明

这样即使 CI 出问题，也不会阻塞业务发版。

## 12. 发版检查清单

发版前：

1. 代码已提交并推送
2. `git status` 干净
3. 本地网页模式验证通过
4. 本地桌面模式验证通过
5. 本地打包成功
6. zip 文件名符合当前约定

发版后：

1. GitHub Release 存在
2. Release 版本号正确
3. Release 说明完整
4. 资产可下载
5. 下载后解压可打开
6. 导入和导出流程可跑通

## 13. 后续可提升方向

如果未来要让发版更稳定，可以考虑：

- 增加自动化 smoke test
- 为桌面应用加入自定义图标
- 做 Apple Developer 签名与 notarization
- 打包 universal 版本
- 增加 changelog 管理
- 在 Actions 中增加构建后校验步骤

这些都不是当前必须项，但会显著提升长期可维护性和交付体验。
