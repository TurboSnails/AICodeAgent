#!/usr/bin/env python3
"""
course_correct.py - 统一 Gradle/Kotlin/Compose/KSP 错误解析器
用法: python3 course_correct.py --log build.log --format json
输出: JSON 格式的错误列表，或纯文本纠错 prompt
"""

import argparse
import json
import re

ERROR_PATTERNS = [
    # Kotlin 编译错误
    (r"e: .+?Unresolved reference: (\w+)", "unresolved_reference", "检查 import 或补充缺失类"),
    (r"e: .+?:(\d+):(\d+) (.+)", "kotlin_error", "Kotlin 编译错误"),
    (r"Type mismatch: inferred type is (.+?) but (.+?) was expected", "type_mismatch", "检查类型兼容性，必要时显式转换或使用正确的泛型参数"),
    (r"Cannot find a parameter with this name: (\w+)", "named_param_missing", "检查函数签名，移除或修正命名参数"),
    (r"None of the following functions can be called with the arguments supplied", "overload_mismatch", "检查函数重载签名，确保参数类型和数量匹配"),
    (r"Variable expected", "variable_expected", "赋值目标必须是可变变量 (var)，不能是 val 或表达式结果"),
    (r"Val cannot be reassigned", "val_reassign", "将 val 改为 var，或重新设计赋值逻辑"),
    (r"Smart cast to '(.+?)' is impossible", "smart_cast_impossible", "添加显式类型转换 (as?) 或使用 when + is 判空"),
    (r"Only safe \(.+?\) or non-null asserted \(.+?\) calls are allowed", "null_safety", "使用 ?. 安全调用、?:  Elvis 运算符或 !! 非空断言"),
    (r"Null can not be a value of a non-null type", "null_nonnull", "确保赋值前对象已初始化，或改用可空类型"),

    # Compose 特化错误
    (r"Composable calls are not allowed", "composable_context", "检查是否在非 @Composable 上下文中调用 Compose 函数"),
    (r"@Composable invocations can only happen from the context of a @Composable function", "composable_invocation", "确保调用方也是 @Composable 函数，或将逻辑提取到 Composable 中"),
    (r"Functions which invoke @Composable functions must be marked with the @Composable annotation", "composable_annotation_missing", "在调用 Compose 函数的父函数上添加 @Composable 注解"),
    (r"The '(.+?)' composable is not a direct or indirect child of a '(.+?)' composable", "composable_scope", "确保在正确的 CompositionLocalProvider / Scaffold 作用域内调用"),

    # KSP / 代码生成
    (r"SiteThemeRegistry.*empty", "ksp_empty", "KSP 单元测试任务被禁用，检查测试代码是否触发了 KSP 重新生成"),
    (r"cannot find symbol|Symbol not found.*SiteThemeRegistry", "ksp_symbol", "检查 KSP 是否正确生成了 Theme Registry；确认 kspDebugKotlin 已执行"),
    (r"KSP: .*error", "ksp_error", "检查 KSP 注解处理器日志，确认注解使用正确"),

    # 资源与字符串
    (r"Missing string resource", "missing_resource", "在 strings.xml 或 siteRes 下补充缺失字符串"),
    (r"Android resource linking failed", "resource_link", "检查资源文件命名、路径和引用是否正确，确认无重复 ID"),
    (r"AAPT: error: resource (.+?) not found", "aapt_missing", f"补充缺失资源 {r'\1'}"),
    (r"error: failed linking references", "link_refs", "检查 Android 资源引用链，确认所有引用的资源存在"),

    # Import / 包名 / 模块
    (r"Unused import", "unused_import", "删除未使用的 import"),
    (r"Package directive does not match the file location", "package_mismatch", "修正 package 声明与文件实际目录一致"),
    (r"Cannot access '(.+?)': it is (private|internal|protected) in '(.+?)'", "visibility", "检查访问权限，必要时提升为 public/internal，或使用同模块内的公开 API"),

    # Gradle / 构建
    (r"ImmutableList not found", "immutable_list", "检查 libs.versions.toml 是否已声明 kotlinx-collections-immutable"),
    (r"Could not resolve all task dependencies for configuration", "gradle_dependency", "检查依赖声明和网络连接，确认仓库可访问"),
    (r"Task '.+?' not found in project", "gradle_task_missing", "检查 Gradle 任务名拼写，确认模块存在"),
    (r"What went wrong:\n(.+?)(?=\nTry:)", "gradle_error", "Gradle 执行错误"),
    (r"Execution failed for task '.+?'", "gradle_task_fail", "Gradle 任务执行失败，查看上层错误堆栈"),
    (r"Could not create task .+?\n> (.+)", "gradle_create_task", "Gradle 任务配置失败，检查 build.gradle.kts 语法"),

    # 测试 / Robolectric
    (r"NullPointerException", "npe", "使用 Robolectric RuntimeEnvironment.getApplication() 获取 Application 上下文，或检查初始化顺序"),
    (r"java\.lang\.IllegalStateException", "illegal_state", "检查对象生命周期和状态机转换，确认在正确阶段调用 API"),
    (r"java\.lang\.ClassNotFoundException", "class_not_found", "检查依赖是否已引入，或类名/包名拼写错误"),
    (r"No tests found for given includes", "no_tests", "检查测试类/方法命名和 @Test 注解，确认测试源集配置正确"),
    (r"Process 'Gradle Test Executor .+?' finished with non-zero exit value", "test_executor_crash", "测试进程崩溃，检查测试中的内存泄漏或主线程阻塞"),

    # 协程 / Flow
    (r"Suspend function '(.+?)' should be called only from a coroutine or another suspend function", "suspend_call", "在协程作用域 (lifecycleScope/viewModelScope) 中调用，或将调用方标记为 suspend"),
    (r"Flow invariant is violated", "flow_invariant", "确保 Flow 操作符在正确的协程上下文中使用，避免跨线程发射"),
]


def parse_errors(log_path: str) -> list[dict]:
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    errors = []
    seen = set()
    for pattern, err_type, suggestion in ERROR_PATTERNS:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            key = (err_type, match.group(0))
            if key in seen:
                continue
            seen.add(key)
            errors.append({
                "type": err_type,
                "match": match.group(0),
                "suggestion": suggestion,
                "line": content[:match.start()].count("\n") + 1
            })
    if not errors:
        lines = content.splitlines()[-30:]
        errors.append({"type": "unknown", "match": "\n".join(lines), "suggestion": "请分析日志", "line": 0})
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--requirement", default="")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    errors = parse_errors(args.log)
    if args.format == "json":
        print(json.dumps(errors, ensure_ascii=False, indent=2))
    else:
        print(f"发现 {len(errors)} 个错误:")
        for e in errors[:10]:
            print(f"  [{e['type']}] {e['match'][:100]}\n    建议: {e['suggestion']}")


if __name__ == "__main__":
    main()
