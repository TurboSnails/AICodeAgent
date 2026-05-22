"""
统一异常层次 — V4 重构
所有业务异常都必须继承 AgentException，便于 Engine 统一捕获和处理。
"""


class AgentException(Exception):
    """V4 业务异常基类"""
    pass


class AgentRecoverableError(AgentException):
    """
    可恢复错误：任务进入 correcting 阶段重试。
    Engine 捕获后自动流转到 correcting，不标记为 FAILED。
    """
    pass


class AgentFatalError(AgentException):
    """
    致命错误：任务直接流转到 FAILED，不再重试。
    """
    pass


# ------------------------------------------------------------------
# AIClient 相关异常
# ------------------------------------------------------------------

class AgentTimeoutError(AgentRecoverableError):
    """AI 调用超时（可重试）"""
    pass


class AgentContextLengthError(AgentFatalError):
    """Prompt 超出模型上下文长度（重试无意义）"""
    pass


class AgentEmptyOutputError(AgentRecoverableError):
    """模型返回空输出（可能为临时故障，可重试）"""
    pass


class AgentRateLimitError(AgentRecoverableError):
    """触发 Rate Limit（可指数退避重试）"""
    pass


class AgentCliUnavailableError(AgentFatalError):
    """claude CLI 未安装或不可用"""
    pass


# ------------------------------------------------------------------
# BuildService 相关异常
# ------------------------------------------------------------------

class BuildFailureError(AgentRecoverableError):
    """Gradle 构建失败（进入 correcting）"""
    pass


# ------------------------------------------------------------------
# GitService / PrService 相关异常
# ------------------------------------------------------------------

class GitCommandError(AgentRecoverableError):
    """Git 命令执行失败"""
    pass


class PrCreationError(AgentRecoverableError):
    """PR 创建失败"""
    pass


class DependencyViolationError(AgentRecoverableError):
    """检测到未批准的依赖变更"""
    pass


# ------------------------------------------------------------------
# Debate / Consensus 相关异常
# ------------------------------------------------------------------

class DebateTimeoutError(AgentRecoverableError):
    """Multi-Agent Debate 超时"""
    pass


class ConsensusValidationError(AgentRecoverableError):
    """Consensus 结构化校验失败"""
    pass
