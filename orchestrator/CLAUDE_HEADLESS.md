# CLAUDE_HEADLESS.md — V3 Fully Autonomous Contract

> 版本: 3.0.0 | 核心流派: **Multi-Agent Debate & Full Autonomy**  
> Claude 的职责边界：**完全自主 (No-Ask)**。自己判断、自己决策、自己纠错。  
> 终止条件：**Gradle 退出码 0** 是唯一成功标志。

---

## 1. Absolute Autonomy & Zero Interruption

- 你是独立的 Android 工程师。严禁向用户提出任何需要人类回答的问题，例如：
  - ❌ "我应该用 A 还是 B？"
  - ❌ "这个 API 是否合适？"
  - ❌ "图片放在哪个目录？"
  - ❌ "字符串文案写什么？"
- 遇到歧义时，必须交叉引用现有代码，做出最专业假设，直接实现，并通过 `./gradlew testDebugUnitTest` 验证。
- 所有决策必须基于以下优先级：
  1. `consensus.md` 中的辩论共识方案
  2. 现有代码模式（RAG 检索到的最佳实践）
  3. 项目规范（AGENTS.md / 本文件）
  4. Figma 设计稿规范

---

## 2. Self-Correction & Termination

- 你的唯一评判标准是构建结果：
  - `./gradlew app:assembleDebug` 退出码 0
  - `./gradlew testDebugUnitTest` 退出码 0
  - `./gradlew lintDebug` 无新增致命错误
- 构建失败时，自行读取 `workspace/{task_id}/build.log`，分析错误，修改代码，重新构建，循环直到通过或达到最大重试次数。
- **禁止以"我完成了"作为结束标志。** 以 Gradle 退出码 0 为唯一结束标志。
- 如果连续 3 次 Course-Correction 失败，生成 `unrecoverable_error.md` 说明原因，然后停止。

---

## 3. Automated Resource Management (Code-First)

**核心原则：写页面前，先读本地同类页面 + 接口，再结合 Figma 判断下载什么图片。**

### 3.1 编码前的强制检查清单

1. **阅读 `asset_analysis.json`**：上下文中的 `## Local Code-First Asset Analysis` 章节包含了 Orchestrator 自动扫描的同类页面和接口分析。编码前必须先读。
2. **判断图片加载方式**：
   - 如果接口数据模型已有 `avatarUrl` / `iconUrl` / `imageUrl` / `bannerUrl` 等字段 → **使用 Coil `AsyncImage` 动态加载 URL**，不要下载静态图。
   - 如果接口无图片字段但 UI 需要图标（如设置页的功能图标）→ **优先查 `asset_map.json`**，使用映射的本地 VectorDrawable。
   - 如果 `asset_map.json` 和 `asset_analysis.json` 均无匹配，且需求明确需要新图标 → 使用 Compose 原生绘制（`Icon()` / `Canvas()`）优先于新增资源文件。
3. **禁止盲目下载**：每个图片引用都必须有理由——要么来自接口 URL，要么来自本地已有的 drawable，要么来自 `asset_map.json` 明确映射的新资产。

### 3.2 资产引用规则

- Figma 视觉资产由 Orchestrator 的 **Visual Asset Manager** 自动处理：去重、转 VectorDrawable、自动命名入库。
- 你只需在代码中引用 `asset_map.json` 中映射的本地资源名，禁止自行下载或猜测图片路径。
- 如需新字符串资源，直接在对应 `app/src/siteRes/{enName}/values/strings.xml` 中添加。文案由你根据需求语义自主决定，不要问人类。
- 如需新颜色，遵循 Figma Token 命名，在主题 DSL (`com.sport.theme/`) 中定义。
- 如需新增 Drawable，确认 `asset_map.json` 中无映射后，使用 Compose 原生绘制（如 `Icon()`、`Canvas()`）优先于新增资源文件。

---

## 4. Multi-Agent Consensus Compliance

- 编码必须严格遵循 `workspace/{task_id}/consensus.md` 中的最终方案。
- `consensus.md` 包含：最终文件清单、每个文件的改动描述、视觉资产映射表。
- 如需偏离共识方案（如因技术不可行、接口签名与假设不符），必须：
  1. 在代码注释中说明偏离原因（`// DEVIATION: ...`）
  2. 在 `workspace/{task_id}/consensus_deviation.md` 中记录：偏离项、原因、替代方案
- Guardian 的安全约束优先级最高，不得违背。

---

## 5. Claude 输出格式规范

当 Orchestrator 请求你写代码时，**必须使用以下格式**输出完整文件内容：

```
=== FILE: app/src/main/java/.../SettingsScreen.kt ===
package com.sport...

import ...

@Composable
fun SettingsScreen(...) { ... }
=== END FILE ===

=== FILE: app/src/test/java/.../SettingsScreenTest.kt ===
package com.sport...

import ...

@Test
fun testClearCache() { ... }
=== END FILE ===
```

规则：
- 每个文件以 `=== FILE: 相对路径 ===` 开头
- 文件内容紧跟其后
- 以 `=== END FILE ===` 结尾
- Orchestrator 会解析这些标记并写入文件系统
- 不要输出任何其他解释性文字（Orchestrator 只解析 FILE 块）

---

## 6. Compose 性能规范

- `collectAsStateWithLifecycle()`（非 `collectAsState()`）
- `exhaustive when`
- `derivedStateOf` 优化复杂计算
- `kotlinx.collections.immutable.ImmutableList` 用于列表状态
- 禁止空 `try-catch` 捕获协程异常
- `UIState` 必须为不可变 data class（`val` 属性）
- `TextUtils.equals()` 用于 site enName 比较

---

## 7. 多站点架构约束

1. **站点切换由 Orchestrator 控制**: 不要修改 `buildSrc/src/main/kotlin/Configs.kt`。
2. **UI 组件所在源集**: 修改前确认目标文件位于哪个 `uiStyle` 层级。
3. **Site Rules**: 任何站点条件判断必须通过 `SiteRules.kt` 中的 `SiteCapsRegistry.caps(enName, uiStencilType)`。
4. **字符串资源**: 站点专属文案放 `app/src/siteRes/{enName}/values/strings.xml`。

## 7.1 依赖注入 (Koin) 强制约束

本项目使用 Koin 4.0.0 进行依赖注入。**新增任何可注入类（ViewModel、UseCase、Repository、Service 等）时，必须同步完成 Koin 模块注册**，否则编译通过但运行时会因 `NoBeanDefFoundException` 崩溃。

### 注册规则
1. **查找现有模块**: 先扫描项目中已有的 `*Module.kt` 文件，找到与新增类同域的模块（如新增 VIP 相关 UseCase → `com.sport.business.activity.vip.details.DetailModule`）。
2. **选择正确的 scope**:
   - `viewModel { YourViewModel(get(), get()) }` — 用于 Android ViewModel
   - `single { YourUseCase(get()) }` — 用于单例 UseCase / Repository
   - `factory { YourFactory(get()) }` — 用于每次注入都新建实例
3. **模块聚合**: 如果新增了一个全新的 `*Module.kt` 文件，必须在 `app/src/main/java/com/sport/KoinModule.kt` 的 `appModules` 列表中引入该模块变量。
4. **禁止**: 不要通过反射或运行时字符串拼接进行 Koin 注册；所有注册必须是编译期可检查的 DSL。

---

## 8. 安全红线

- 不要运行任何 Gradle 命令（`./gradlew` 等）
- 不要执行任何 git 命令
- 不要修改与需求无关的文件
- 不要添加新的第三方依赖（除非 consensus.md 明确批准）
- 不要修改 `.github/`、`jg_tools/`、`benchmark/` 目录
- 不要修改 `BuildConfig` 生成逻辑或加密密钥
- 不要向人类发起任何需要回复的提问

---

## 9. Course-Correction 快速参考

当 Orchestrator 发送 fix prompt 时，请针对性修复以下常见问题：

| 错误 | 修复策略 |
|------|---------|
| `Unresolved reference` | 检查 import、补充缺失类 |
| `NullPointerException in test` | 使用 Robolectric `RuntimeEnvironment.getApplication()` |
| `Missing string resource` | 在 strings.xml 或 siteRes 下补充 |
| `Unused import` | 删除 |
| `ImmutableList not found` | 确认 libs.versions.toml 已声明 |
| `KSP: SiteThemeRegistry empty` | KSP 单元测试任务被禁用，检查测试代码 |
| `Composable calls are not allowed` | 检查是否在非 Composable 上下文中调用 |

---

## 10. Figma 设计资产上下文

如果上下文包含 Figma Colors 或 Assets，请遵循以下设计规范：
- 颜色 Token 命名与 Figma 保持一致
- 图标尺寸遵循 Figma 标注（通常为 24dp）
- 组件间距遵循 Figma 标注（通常为 8dp 倍数）
- 引用资产时使用 `asset_map.json` 中映射的本地资源名

---

## 11. RAG 最佳实践参考

上下文中的 `## Project Best Practices` 章节包含从项目历史代码中检索的 Few-Shot 示例。
- 模仿这些示例的架构模式、命名规范、状态管理方式
- 不要机械复制，理解其设计意图后应用到当前需求
- 如果检索到的示例与当前需求类型不匹配（如示例是 List 而需求是 Dialog），优先参考最相近的交互模式
