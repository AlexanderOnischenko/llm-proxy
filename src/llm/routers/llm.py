# app/routers/llm.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from openai import OpenAIError

import re
import inspect
from typing import Any, List


from langchain_core.messages import BaseMessage as SchemaBase
from langchain_core.messages import HumanMessage as SchemaHuman
from langchain_core.messages import AIMessage as SchemaAI
from langchain_core.messages import SystemMessage as SchemaSystem
from langchain_core.outputs import LLMResult

from llm.deps import get_settings, get_llm, CachedQueuedChatOpenAI
from llm.models.llm import LLMRequest
from shared_pkg.utils.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter()

@router.get("/health")
def health(settings=Depends(get_settings)):
    return {"status": "ok", "env": settings.environment}

def _validate_message_content(raw: Any) -> str | list[str | dict[str, Any]]:
    if raw is None:
        raise HTTPException(400, "Message content cannot be null")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        validated: list[str | dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if isinstance(item, str):
                validated.append(item)
            elif isinstance(item, dict):
                validated.append(item)
            else:
                raise HTTPException(400, f"Invalid list item at index {idx}: {item!r}")
        return validated
    raise HTTPException(400, f"Unsupported message content type: {type(raw).__name__}")

def extract_messages(nested_list):
    flat_list = []
    if isinstance(nested_list, list):
        for item in nested_list:
            if "messages" in item:
                flat_list.extend(extract_messages(item["messages"]))
        if flat_list:
            return flat_list
    return nested_list


def split_system_message(text: str) -> list[str]:
    """
    Разбивает большой системный текст на части по маркеру "# PROMPT:".
    Если маркер не найден — возвращает список из одного элемента.
    """
    # (?m) — enable multiline, (?=^# PROMPT:) — разделяем, сохранив маркер во второй части
    parts = re.split(r'(?m)(?=^# PROMPT:)', text)
    return [p.strip() for p in parts if p.strip()]

@router.post("/llm", response_class=JSONResponse)
async def call_llm(req: LLMRequest, llm: CachedQueuedChatOpenAI = Depends(get_llm)) -> JSONResponse:
    logger.info(f"LLM request: sender={req.sender!r} method={req.method!r} args={req.args} kwargs={list(req.kwargs)}")

    # 1) Фильтруем «безопасные» ключи
    safe = {
        k: v
        for k, v in req.kwargs.items()
        if k in ("input", "prompt", "messages", "stop", "callbacks", "tags", "metadata", "run_manager")
    }
    # 2) Если есть сырой список dict в messages — превращаем его в правильные объекты
    if "messages" in safe:
        messages = extract_messages(safe["messages"])
        norm_msgs: List[SchemaBase] = []
        for m in messages:
            if isinstance(m, dict):
                t = m.get("type", "system")
                c = _validate_message_content(m.get("content"))
                if t == "system":
                    norm_msgs.append(SchemaSystem(content=c))
                elif t in ("user", "human"):
                    norm_msgs.append(SchemaHuman(content=c))
                elif t in ("assistant", "ai"):
                    norm_msgs.append(SchemaAI(content=c))
                else:
                    raise HTTPException(400, f"Unknown message type: {t}")
            elif isinstance(m, SchemaBase):
                norm_msgs.append(m)
            else:
                raise HTTPException(400, f"Invalid message element: {m!r}")
        safe["messages"] = norm_msgs

    # 3) Remap invoke → generate если передали messages
    method_name = req.method
    if method_name == "invoke" and "messages" in safe:
        method_name = "generate"
        #req.args.append('method_name="invoke"')
        logger.info(" ↳ remapping invoke → generate because messages were provided")

    method_name = "a"+method_name

    if not hasattr(llm, method_name):
        raise HTTPException(400, f"Unknown LLM method: {method_name}")
    fn = getattr(llm, method_name)

    # 4) Оставляем только параметры, которые ждёт fn
    sig = inspect.signature(fn)
    call_kwargs = {k: v for k, v in safe.items() if k in sig.parameters}

    try:
        # THIS RETURNS A ChatResult object
        result: LLMResult = await fn(*req.args, **call_kwargs)
        # FastAPI will .dict() it for you under the hood
        d = result.model_dump()
        payload = jsonable_encoder(d)
        return JSONResponse(content=payload)
    except OpenAIError as e:
        # ❶ печатаем КОРОТКОЕ описание + traceback
        logger.exception("OpenAI upstream failure (%s): %s", e.__class__.__name__, e)
        # ❷ клиенту отвечаем нейтральным 502 без HTML
        raise HTTPException(
            status_code=502,
            detail=f"Upstream LLM error: {e.__class__.__name__} {getattr(e, 'status_code', '')}"
        ) from e
    except Exception as e:
        logger.exception("LLM request failed")
        raise HTTPException(status_code=502, detail=str(e))
