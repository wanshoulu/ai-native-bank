"""
智能体安全防护服务的FastAPI服务器

本模块负责FastAPI应用初始化、生命周期管理和全局中间件。
所有外部交互的API端点已拆分到 src/api/ 目录下的各功能模块中。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.config import (
    setup_logging, SERVER_CONFIG, GUARDRAILS_DEFAULT_RULES,
    SECURITY_CHECK_PROMPT, CHECKPOINTER_DB_PATH,
    ALLOWED_ORIGINS, MAX_REQUEST_SIZE, STATIC_DIR,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from src.agent import get_agent, LangChainAgent
from src.classifier.categories import DEFAULT_THRESHOLDS, CATEGORY_ANCHORS
from src.classifier.service import get_classifier_service
from database import (
    init_db, close_db, get_db,
    GuardrailRuleCRUD, PIIConfigCRUD,
    PromptDefenseConfigCRUD, WhitelistRuleCRUD,
    ContentFilterConfigCRUD, ContentFilterCategoryCRUD, ContentFilterAnchorCRUD,
)
from src.api import register_routers
from src.api import state as api_state
from src.observability import init_observability, shutdown_observability

# 设置日志
setup_logging()
logger = logging.getLogger("agent_service")
security_logger = logging.getLogger("security")


# 应用生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理，处理启动和关闭事件"""
    global agent

    # 初始化 Phoenix 链路观测（需在 Agent 创建前完成 LangChain 自动埋点，失败静默降级）
    init_observability()

    # 启动时初始化数据库
    logger.info("正在初始化数据库...")
    await init_db()
    logger.info("数据库初始化完成")

    # 初始化系统默认规则
    logger.info("正在初始化系统默认规则...")
    async for db in get_db():
        try:
            inserted_count = await GuardrailRuleCRUD.initialize_system_rules(db, GUARDRAILS_DEFAULT_RULES)
            await db.commit()  # 提交事务
            if inserted_count > 0:
                logger.info(f"已插入 {inserted_count} 条系统默认规则")
            else:
                logger.info("系统默认规则已存在，跳过初始化")
        except Exception as e:
            await db.rollback()  # 回滚事务
            logger.error(f"初始化系统规则失败: {str(e)}")
            raise
        break

    # 初始化PII配置（从数据库加载，默认禁用）
    logger.info("正在初始化PII配置...")
    pii_config_obj = None
    async for db in get_db():
        try:
            pii_config_obj = await PIIConfigCRUD.get_or_create_config(db)
            await db.commit()
            logger.info(f"PII配置加载完成: input_enabled={pii_config_obj.input_enabled}, output_enabled={pii_config_obj.output_enabled}")
            break
        except Exception as e:
            await db.rollback()
            logger.error(f"PII配置初始化失败: {str(e)}", exc_info=True)
            raise

    # 初始化提示词防御配置（从数据库加载，首次启动时从 config.py 写入默认值）
    logger.info("正在初始化提示词防御配置...")
    prompt_config_obj = None
    async for db in get_db():
        try:
            prompt_config_obj = await PromptDefenseConfigCRUD.get_or_create_config(
                db, default_prompt=SECURITY_CHECK_PROMPT
            )
            await db.commit()
            logger.info(f"提示词防御配置加载完成: enabled={prompt_config_obj.enabled}")
            break
        except Exception as e:
            await db.rollback()
            logger.error(f"提示词防御配置初始化失败: {str(e)}", exc_info=True)
            raise

    # 从数据库加载所有GuardRails规则（system + custom）
    logger.info("正在加载GuardRails规则...")
    rules_for_agent = []
    async for db in get_db():
        try:
            rules_for_agent = await GuardrailRuleCRUD.get_rules_for_agent(db)
            logger.info(f"从数据库加载了 {len(rules_for_agent)} 条GuardRails规则")
            break
        except Exception as e:
            logger.error(f"加载GuardRails规则失败: {str(e)}", exc_info=True)
            raise

    # 初始化持久化 checkpointer
    logger.info(f"正在初始化对话持久化存储: {CHECKPOINTER_DB_PATH}")
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB_PATH)) as checkpointer:
        logger.info("对话持久化存储初始化成功")

        # 初始化智能体（传入数据库中的规则，避免 uvicorn reload 重复创建）
        try:
            agent = get_agent(
                custom_guardrail_rules=rules_for_agent,
                prompt_defense_config=prompt_config_obj.to_dict() if prompt_config_obj else None,
                checkpointer=checkpointer
            )
            api_state.set_agent(agent)
            logger.info("Agent 初始化成功")
        except Exception as e:
            logger.error(f"Agent 初始化失败: {str(e)}", exc_info=True)
            raise

        # 初始化白名单规则（从数据库加载）
        logger.info("正在初始化白名单规则...")
        async for db in get_db():
            try:
                whitelist_rules = await WhitelistRuleCRUD.get_all_rules(db, active_only=True)
                whitelist_patterns = [rule.pattern for rule in whitelist_rules]
                if agent:
                    agent.reload_white_list_rules(whitelist_patterns)
                logger.info(f"白名单规则加载完成: 共 {len(whitelist_patterns)} 条规则")
                break
            except Exception as e:
                logger.error(f"白名单规则初始化失败: {str(e)}", exc_info=True)
                raise

        # Agent 初始化完成后，将PII配置加载到 Agent
        if agent and pii_config_obj:
            agent.reload_pii_config(pii_config_obj.to_dict())

        # 初始化内容过滤配置（从数据库加载，首次启动时使用默认值）
        logger.info("正在初始化内容过滤配置...")
        cf_config_obj = None
        async for db in get_db():
            try:
                cf_config_obj = await ContentFilterConfigCRUD.get_or_create_config(db)
                await db.commit()
                logger.info(
                    f"内容过滤配置加载完成: input_enabled={cf_config_obj.input_enabled}, "
                    f"output_enabled={cf_config_obj.output_enabled}, action_mode={cf_config_obj.action_mode}"
                )
                break
            except Exception as e:
                await db.rollback()
                logger.error(f"内容过滤配置初始化失败: {str(e)}", exc_info=True)
                raise

        # 初始化内容过滤类别和锚点（首次启动时从 categories.py 加载到数据库）
        logger.info("正在初始化内容过滤类别和锚点...")
        async for db in get_db():
            try:
                # 准备默认类别数据（含阈值）
                default_categories = {
                    name: {"threshold": DEFAULT_THRESHOLDS.get(name, 0.75), "description": f"系统默认类别: {name}"}
                    for name in DEFAULT_THRESHOLDS.keys()
                }

                # 初始化系统类别
                cat_count = await ContentFilterCategoryCRUD.initialize_system_categories(db, default_categories)
                if cat_count > 0:
                    logger.info(f"已创建 {cat_count} 个默认内容过滤类别")

                # 初始化系统锚点
                anchor_count = await ContentFilterAnchorCRUD.initialize_system_anchors(db, CATEGORY_ANCHORS)
                if anchor_count > 0:
                    logger.info(f"已创建 {anchor_count} 个默认内容过滤锚点")

                await db.commit()
                break
            except Exception as e:
                await db.rollback()
                logger.error(f"内容过滤类别/锚点初始化失败: {str(e)}", exc_info=True)
                raise

        # 向量化缺少 embedding 的锚点
        logger.info("正在检查并向量化锚点...")
        async for db in get_db():
            try:
                cf_service = get_classifier_service()
                vectorized = await cf_service.vectorize_missing_anchors(db)
                if vectorized > 0:
                    logger.info(f"已向量化 {vectorized} 个锚点")
                await db.commit()
                break
            except Exception as e:
                await db.rollback()
                logger.error(f"锚点向量化失败: {str(e)}", exc_info=True)
                raise

        # 从数据库加载内容过滤数据到内存
        logger.info("正在从数据库加载内容过滤数据到内存...")
        async for db in get_db():
            try:
                cf_service = get_classifier_service()
                loaded = await cf_service.load_from_db(db)
                if loaded:
                    logger.info("内容过滤数据已加载到内存")
                else:
                    logger.warning("内容过滤数据加载失败，内容过滤功能可能不可用")
                break
            except Exception as e:
                logger.error(f"加载内容过滤数据到内存失败: {str(e)}", exc_info=True)
                raise

        # 将内容过滤配置加载到 Agent
        if agent and cf_config_obj:
            agent.reload_content_filter_config(cf_config_obj.to_dict())

        logger.info(f"服务已启动完成: http://{SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")

        yield

        # 关闭时刷新未上报的 trace
        shutdown_observability()

        # 关闭时清理数据库连接
        logger.info("正在关闭数据库连接...")
        await close_db()
        logger.info("数据库连接已关闭")


# 初始化FastAPI应用
app = FastAPI(
    title="智能体安全防护后端服务",
    description="基于LangChain和GuardRails AI的安全防护智能体服务",
    version="1.0.0",
    lifespan=lifespan
)

# 添加CORS中间件
# 在生产环境中，应该明确指定允许的域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 请求大小验证中间件
@app.middleware("http")
async def validate_request_size(request: Request, call_next):
    """
    验证请求大小的中间件
    防止过大的请求体导致服务器资源耗尽
    """
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": f"请求体过大，最大允许{MAX_REQUEST_SIZE}字节"}
            )
    response = await call_next(request)
    return response

# 挂载静态文件
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 智能体实例（在 lifespan 中延迟初始化，避免 uvicorn reload 模式重复创建）
agent: LangChainAgent | None = None

# 注册API路由（所有外部交互端点已拆分到 src/api/ 各模块）
register_routers(app)

# 错误处理器
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """处理HTTP异常并记录日志"""
    logger.warning(f"HTTP异常: {exc.status_code} - {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """处理一般异常并记录日志"""
    # 忽略Windows网络断开错误
    if isinstance(exc, OSError) and getattr(exc, 'winerror', None) == 64:
        # WinError 64: 指定的网络名不再可用 - 客户端断开连接
        return JSONResponse(
            status_code=499,  # Client Closed Request
            content={"detail": "客户端已断开连接"}
        )

    logger.error(f"未处理的异常: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "内部服务器错误"}
    )

if __name__ == "__main__":
    import uvicorn

    # 自定义日志过滤器，抑制Windows网络断开错误
    class NetworkErrorFilter(logging.Filter):
        def filter(self, record):
            # 只过滤掉Windows网络断开相关的特定错误日志
            msg = record.getMessage()

            # 过滤WinError 64相关错误
            if 'WinError 64' in msg:
                return False

            # 过滤socket accept失败
            if 'Accept failed on a socket' in msg:
                return False

            # 过滤Task exception was never retrieved（通常是网络断开导致）
            if 'Task exception was never retrieved' in msg:
                # 检查是否是OSError相关的异常
                if hasattr(record, 'exc_info') and record.exc_info:
                    exc_type = record.exc_info[0]
                    if exc_type and exc_type.__name__ == 'OSError':
                        return False

            return True

    # 应用过滤器到asyncio和uvicorn的日志器
    for logger_name in ['asyncio', 'uvicorn.error', 'uvicorn.access']:
        log = logging.getLogger(logger_name)
        log.addFilter(NetworkErrorFilter())

    # 设置asyncio的日志级别为WARNING，减少噪音
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    logger.info(f"启动服务器 {SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")
    uvicorn.run(
        "server:app",
        host=SERVER_CONFIG["host"],
        port=SERVER_CONFIG["port"],
        reload=SERVER_CONFIG["reload"],
        log_level="warning"  # 降低uvicorn日志级别
    )
