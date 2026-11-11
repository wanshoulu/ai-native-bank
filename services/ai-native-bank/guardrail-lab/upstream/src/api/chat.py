"""
聊天 API 端点

包含普通聊天、流式聊天和对话历史接口。
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config.config import LOG_STR_MAX
from database import get_db, RequestLogCRUD
from database.connection import async_session_factory
from src.api.models import ChatRequest, ChatResponse
from src.api.utils import validate_message, extract_client_info, log_interaction, analyze_security_result
from src.api import state
from src.observability import request_context

router = APIRouter()
logger = logging.getLogger("agent_service")


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    聊天端点，通过安全防护智能体处理消息

    该端点使用带有GuardRails安全防护的智能体
    """
    try:
        # 验证输入
        validate_message(request.message)

        # 获取客户端信息
        source_ip, user_agent = extract_client_info(http_request)

        # 处理消息（Phoenix 请求根 span + session 绑定，未启用时零开销）
        with request_context(
            conversation_id=request.conversation_id,
            endpoint="chat",
            source_ip=source_ip,
            streaming_mode="non_stream",
        ):
            result = await state.agent.process_message(
                message=request.message,
                conversation_id=request.conversation_id
            )

        # 分析安全检测结果
        is_blocked = result.get("is_blocked", False)
        detect_reason = result.get("block_reason", "")
        detected_by = result.get("blocked_by", None)
        detection_summary = result.get("detection_summary")

        security_analysis = analyze_security_result(
            is_blocked=is_blocked,
            detect_reason=detect_reason,
            detected_by=detected_by,
            violated_rules=result.get("violated_rules", []),
            detection_summary=detection_summary,
        )

        # 保存到数据库
        try:
            await RequestLogCRUD.create_log(
                db=db,
                source_ip=source_ip,
                user_agent=user_agent,
                is_attack=security_analysis.is_attack,
                is_blocked=is_blocked,
                attack_type=security_analysis.attack_type,
                security_risks=security_analysis.security_risks,
                detect_reason=security_analysis.detect_reason if hasattr(security_analysis, 'detect_reason') else detect_reason,
                detected_by=security_analysis.detected_by,
                user_input=request.message,
                response_content=result["response"],
                conversation_id=request.conversation_id,
            )
        except Exception as db_error:
            logger.error(f"保存到数据库失败: {str(db_error)}", exc_info=True)
            # 不影响主流程，继续返回响应

        # 在后台记录交互
        background_tasks.add_task(
            log_interaction,
            request.conversation_id,
            request.message,
            result["response"],
            is_blocked,
            detect_reason
        )

        # 返回响应（检测模式下使用 security_analysis 的检测信息）
        final_detect_reason = security_analysis.detect_reason or detect_reason
        final_detected_by = security_analysis.detected_by or result.get('blocked_by')
        return ChatResponse(
            response=result["response"],
            is_blocked=is_blocked,
            detect_reason=final_detect_reason,
            detected_by=final_detected_by,
            violated_rules=result.get("violated_rules", []),
            response_message=result.get("response_message"),
            conversation_id=result["conversation_id"],
            timestamp=datetime.now().isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"聊天端点错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"内部服务器错误") from e


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    http_request: Request
):
    """
    流式聊天端点

    返回智能体响应的Server-Sent Events (SSE)流
    """
    try:
        # 验证输入
        validate_message(request.message)

        # 为此流生成唯一ID
        stream_id = str(uuid.uuid4())[:8]

        # 获取客户端信息
        source_ip, user_agent = extract_client_info(http_request)

        async def event_generator():
            """为流式响应生成SSE事件（通过 LangGraph 工作流）"""
            full_response = ""
            is_rejected = False
            rejected_info = {}
            detection_summary = None

            try:
                # 记录流开始
                logger.info(f"开始流 {stream_id}，对话ID: {request.conversation_id}")

                # 流式处理消息（Phoenix 请求根 span；标注伪流式/真流式路径）
                streaming_mode = "pseudo" if state.agent._need_output_check() else "real"
                with request_context(
                    conversation_id=request.conversation_id,
                    endpoint="chat_stream",
                    source_ip=source_ip,
                    streaming_mode=streaming_mode,
                ):
                    async for chunk in state.agent.process_message_stream_graph(
                        message=request.message,
                        conversation_id=request.conversation_id
                    ):
                        # 收集响应内容
                        if chunk.startswith("data: ") and not chunk.endswith("[DONE]\n\n"):
                            content = chunk[6:]  # 去掉 "data: " 前缀

                            # 捕获检测摘要事件（内部元数据，不转发给客户端）
                            if content.startswith("[DETECTION_SUMMARY]"):
                                try:
                                    detection_summary = json.loads(content[len("[DETECTION_SUMMARY]"):])
                                except json.JSONDecodeError:
                                    pass
                                continue  # 不输出到客户端，也不计入 full_response

                            full_response += content

                            # 检测是否为工作流拦截响应（仅检测第一个有效 chunk）
                            if not is_rejected and full_response.startswith('{"status":'):
                                try:
                                    json_data = json.loads(full_response)
                                    if json_data.get("status") == "reject" and json_data.get("blocked_by"):
                                        is_rejected = True
                                        rejected_info = json_data
                                except json.JSONDecodeError:
                                    pass

                            yield chunk

                # 发送完成事件（agent 已发送的 [DONE] 已在上面过滤）
                yield "data: [DONE]\n\n"

                # 记录流完成
                logger.info(f"完成流 {stream_id}，对话ID: {request.conversation_id}")

                # 从工作流拦截信息或响应内容构建安全信息
                if is_rejected:
                    is_blocked = True
                    detect_reason = rejected_info.get("detected_issues", "检测到安全风险")
                    detected_by = rejected_info.get("blocked_by", "unknown")
                    violated_rules = rejected_info.get("violated_rules", [])
                    security_analysis = analyze_security_result(
                        is_blocked=True,
                        detect_reason=detect_reason,
                        detected_by=detected_by,
                        violated_rules=violated_rules,
                    )
                else:
                    is_blocked = False
                    security_analysis = analyze_security_result(
                        is_blocked=False,
                        detection_summary=detection_summary,
                    )

                is_attack = security_analysis.is_attack
                attack_type = security_analysis.attack_type
                security_risks = security_analysis.security_risks
                detected_by = security_analysis.detected_by
                detect_reason = security_risks.get("detect_reason") if security_risks else None

                # 保存到数据库
                try:
                    async with async_session_factory() as db:
                        await RequestLogCRUD.create_log(
                            db=db,
                            source_ip=source_ip,
                            user_agent=user_agent,
                            is_attack=is_attack,
                            is_blocked=is_blocked,
                            attack_type=attack_type,
                            security_risks=security_risks,
                            detect_reason=detect_reason,
                            detected_by=detected_by,
                            user_input=request.message,
                            response_content=full_response,
                            conversation_id=request.conversation_id,
                        )
                        await db.commit()
                except Exception as db_error:
                    logger.error(f"保存到数据库失败: {str(db_error)}", exc_info=True)

            except Exception as e:
                logger.error(f"流 {stream_id} 错误: {str(e)}", exc_info=True)
                yield f"data: 流式响应出现错误: {str(e)[:LOG_STR_MAX]}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Stream-ID": stream_id
            }
        )

    except Exception as e:
        logger.error(f"设置流失败: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "设置流式响应失败"}
        )


@router.get("/conversations/{conversation_id}/history")
async def get_conversation_history(conversation_id: str):
    """获取特定对话ID的对话历史（从持久化 checkpointer 中读取）。"""
    try:
        if state.agent is None or state.agent.memory is None:
            raise HTTPException(status_code=503, detail="Agent 尚未初始化完成")

        config = {"configurable": {"thread_id": conversation_id}}
        messages: List[Dict[str, Any]] = []

        # 从 AsyncSqliteSaver 读取该对话的所有 checkpoint，取 messages 最多的版本
        async for checkpoint_tuple in state.agent.memory.alist(config):
            checkpoint = checkpoint_tuple[1] if len(checkpoint_tuple) > 1 else None
            if checkpoint and checkpoint.get("channel_values"):
                ch_values = checkpoint["channel_values"]
                if ch_values.get("messages"):
                    msgs = ch_values["messages"]
                    if len(msgs) > len(messages):
                        messages = msgs

        return {
            "conversation_id": conversation_id,
            "message_count": len(messages),
            "messages": messages,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取对话历史失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取对话历史失败") from e
