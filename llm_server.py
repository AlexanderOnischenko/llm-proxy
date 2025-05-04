from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langchain.schema import HumanMessage as SchemaHuman, SystemMessage as SchemaSystem, AIMessage as SchemaAI, BaseMessage as SchemaBase
from langchain.schema import ChatResult
from llm.llm_wrapper import get_llm  # синглтон-фабрика внутри этого процесса
from utils.logger import logger
from typing import Any, Dict, List
import inspect
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from langchain.schema import LLMResult
import re

def extract_messages(nested_list):
    flat_list = []
    if isinstance(nested_list, list):
        for item in nested_list:
            if "messages" in item:
                flat_list.extend(extract_messages(item["messages"]))
        if flat_list:
            return flat_list
    return nested_list

app = FastAPI()
llm = get_llm()

class LLMRequest(BaseModel):
    method: str
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}
    sender: str = "unknown"

def split_system_message(text: str) -> list[str]:
    """
    Разбивает большой системный текст на части по маркеру "# PROMPT:".
    Если маркер не найден — возвращает список из одного элемента.
    """
    # (?m) — enable multiline, (?=^# PROMPT:) — разделяем, сохранив маркер во второй части
    parts = re.split(r'(?m)(?=^# PROMPT:)', text)
    return [p.strip() for p in parts if p.strip()]

@app.post("/llm", response_class=JSONResponse)
async def call_llm(req: LLMRequest) -> JSONResponse:
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
                t = m.get("type","system")
                c = m.get("content")
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

    if not hasattr(llm, method_name):
        raise HTTPException(400, f"Unknown LLM method: {method_name}")
    fn = getattr(llm, method_name)

    # 4) Оставляем только параметры, которые ждёт fn
    sig = inspect.signature(fn)
    call_kwargs = {k: v for k, v in safe.items() if k in sig.parameters}

    try:
        # THIS RETURNS A ChatResult object
        result: LLMResult = fn(*req.args, **call_kwargs)
        # FastAPI will .dict() it for you under the hood
        d = result.model_dump()
        payload = jsonable_encoder(d)
        return JSONResponse(content=payload)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
