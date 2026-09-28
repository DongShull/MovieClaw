# iOS 首发：账号持有人操作清单

> 2026-09-28。流程与原理见 [ios-release.md](ios-release.md)；本文只列**必须由账号持有人亲手做**的事，
> 按顺序做完一项勾一项。打包在一台新的 Mac 上进行（第 1 步搭环境）。
> 凭证（.p8、.p12、密码）一律只放本机钥匙串 / `~/.appstoreconnect` / GitHub 仓库密钥，**不入库、不贴进聊天或 Issue**。

## 1. 新机器：搭打包环境

- [ ] macOS 26 或更新；App Store 装 **Xcode 26 或更新**（本项目要 iOS 26 SDK 与 Swift 6.2），
      首次打开 Xcode 装完组件后执行 `sudo xcodebuild -license accept`
- [ ] 装 Homebrew，再 `brew install xcodegen`（工程文件由 `apps/apple/project.yml` 生成，不入库）
- [ ] 拉代码：
      ```bash
      mkdir -p ~/workspace && cd ~/workspace
      git clone https://github.com/movieclaw/movieclaw.git && cd movieclaw
      git checkout feat/ios-app
      ```
- [ ] 建本机签名配置（已被 .gitignore 忽略）：
      ```bash
      echo 'DEVELOPMENT_TEAM = <第 2 步确认的 Team ID>' > apps/apple/XcodeConfig/Signing.local.xcconfig
      ```
- [ ] 先编一次模拟器版确认环境：`apps/apple/scripts/build.sh`（`MC_SIM` 指定模拟器名，默认 iPhone 17；
      依赖包缓存在 `~/workspace/.mc-ios-spm`，首次要下载 FFmpeg 等二进制，几分钟）

## 2. 开发者账号（developer.apple.com）

- [ ] Membership 页确认**付费团队的 Team ID**，填进上面的 `Signing.local.xcconfig`。
      旧机器上配的是 `G4LW3GC2T7`，要确认它就是付费团队而不是以前的个人免费团队
- [ ] Xcode → 设置 → 账户：登录这个 Apple ID（选下一步的 API 密钥也建议登录，真机调试要用）
- [ ] Identifiers：注册 App ID `io.movieclaw.app`（也可以留给 Xcode 首次导出时自动注册）

## 3. App Store Connect

- [ ] 用户和访问 → 集成 → App Store Connect API → 生成**团队密钥**，角色选「App 管理」。
      下载 `.p8`（只能下载一次），放到 `~/.appstoreconnect/private_keys/AuthKey_<密钥 ID>.p8`，
      记下**密钥 ID** 与 **Issuer ID**。这把密钥同时用于 iOS 上传和 Mac 转码器公证
- [ ] App → 新建 App：平台 iOS、名称 MovieClaw（被占用就换）、主要语言简体中文、
      套装 ID `io.movieclaw.app`、SKU `movieclaw-ios`
- [ ] App 信息：
  - [ ] 隐私政策网址 `https://github.com/movieclaw/movieclaw/blob/main/docs/privacy-policy.md`
        （合入 main 前临时用 `…/blob/feat/ios-app/docs/privacy-policy.md`）
  - [ ] 技术支持网址 `https://github.com/movieclaw/movieclaw/issues`
  - [ ] 类别「娱乐」，价格免费
  - [ ] App 隐私问卷：**不收集数据**
  - [ ] 年龄分级问卷（「不受限制的网络访问」选是）
- [ ] TestFlight → 内部测试：新建内部测试组，把自己加进去；手机装 TestFlight App

## 4. 第一次上传（完整版 → 内部 TestFlight）

- [ ] 先只导出不上传，验证签名：
      ```bash
      cd apps/apple
      export MC_ASC_KEY_ID=<密钥 ID> MC_ASC_ISSUER_ID=<Issuer ID>
      scripts/release.sh            # 成功会打印「已导出 完整版：build-release/…」
      ```
      首次会在账号下自动创建「Apple Distribution」证书与描述文件，属正常流程
- [ ] 上传：`scripts/release.sh --upload`
- [ ] 等 5～30 分钟，TestFlight 里出现构建后装到手机
- [ ] **看 Apple 发来的邮件**：若有 ITMS-91053（隐私清单缺声明）或其他警告，把邮件原文发过来处理
- [ ] 打包产物在 `apps/apple/build-release/`（约 2 GB），确认没问题后可删

## 5. 对外测试 / 上架前（可以晚点做）

- [ ] 决定**演示服务器**放哪：公网可达、HTTPS，不用自己的真实 NAS；只放开放授权片源
      （Big Buck Bunny、Sintel、Tears of Steel），不接资源站点与下载器，建一个给审核员的成员账号
- [ ] 商店版上传：`scripts/release.sh --store --upload`，这个构建才能加入对外测试组 / 提审
- [ ] 6.9 英寸 iPhone 截图至少 3 张（用演示服务器内容截）
- [ ] 审核信息填演示服务器地址、账号与备注（模板见 ios-release.md §4）

## 6. Mac 转码器签名与公证（分支 feat/transcoder-notarize 合入 main 后生效）

- [ ] developer.apple.com → Certificates → 新建 **Developer ID Application**（只有账号持有人能建），
      在「钥匙串访问」里连私钥导出为 `.p12` 并设密码
- [ ] GitHub 仓库 → Settings → Secrets and variables → Actions，新增：
  - `MACOS_DEVELOPER_ID_P12`：`base64 -i developer-id.p12 | pbcopy` 的内容
  - `MACOS_DEVELOPER_ID_P12_PASSWORD`：导出时设的密码
  - `APPLE_API_KEY_P8`：第 3 步 `.p8` 文件的全文
  - `APPLE_API_KEY_ID` / `APPLE_API_ISSUER_ID`
- [ ] 下次发版后下载 `MovieClawTranscoder-macos-arm64.zip`，双击能直接打开即成功
      （本机手工打包的方法见 `macos/MovieClawTranscoder/README.md`）
