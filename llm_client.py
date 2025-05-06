import httpx
import asyncio
from typing import List, Optional, Any, Union
from pydantic import PrivateAttr
from langchain_core.language_models.base import BaseLanguageModel
from utils.logger import logger
from functools import wraps
from langchain_core.prompts.chat import ChatPromptValue
from langchain_core.messages.base import BaseMessage
from langchain_core.messages.human import HumanMessage
from langchain.schema import LLMResult, ChatResult
import langchain_core.outputs.chat_result



# Системные константы из ENV
ENDPOINT = "http://localhost:8001/llm"
TIMEOUT  = 300.0

class RemoteLLM(BaseLanguageModel):
    endpoint: str = ENDPOINT
    timeout: float = TIMEOUT
    _client: httpx.Client = PrivateAttr()

    def __init__(self, **data):
        super().__init__(**data)

    def model_post_init(self, __context: dict) -> None:
        self._client = httpx.Client(timeout=self.timeout)

    @property
    def _llm_type(self) -> str:
        return "remote_llm"

    @property
    def _identifying_params(self) -> dict:
        # <-- now a property, so self._identifying_params is a dict
        return {"endpoint": self.endpoint}

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        sender: Optional[str] = "uknown",
    ) -> LLMResult:
        # если передали обычную строку — упаковываем в HumanMessage
        if isinstance(messages, str):
            messages = [HumanMessage(content=messages)]
        # если передали единичный BaseMessage — превращаем в список
        elif isinstance(messages, BaseMessage):
            messages = [messages]
        # 3) Сериализуем BaseMessage в dict
        serial = [m.dict() for m in messages]
        payload = {
            "method": "generate",
            "args": [],
            "kwargs": {"messages": serial, "stop": stop},
            "sender": sender,
        }
        resp = self._client.post(self.endpoint, json=payload)
        resp.raise_for_status()
        raw = resp.json()    
        # 1) Парсим в LLMResult
        lm_res = LLMResult.parse_obj(raw)
        return lm_res

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        sender : Optional[str] = "unknown"
    ) -> LLMResult:
        async with httpx.AsyncClient(timeout=self.timeout) as ac:
            serial = [m.dict() for m in messages]
            payload = {
                "method": "generate",
                "args": [],
                "kwargs": {"messages": serial, "stop": stop},
                "sender": sender,
            }
            resp = await ac.post(self.endpoint, json=payload)
            resp.raise_for_status()
            raw = resp.json()    

            # 1) Парсим в LLMResult
            lm_res = LLMResult.parse_obj(raw)
            return lm_res
        
    # Работа с ChatPromptValue
    def generate_prompt(
        self,
        prompt: Union[ChatPromptValue, List[BaseMessage]],
        stop: Optional[List[str]] = None,
        run_manager=None,
        callbacks: Any = None,
        **kwargs: Any,
    ) -> LLMResult:
        # если это уже список BaseMessage — возьмём его,
        # иначе попытаемся вызвать .to_messages()
        if isinstance(prompt, list):
            messages = prompt
        else:
            messages = prompt.to_messages()

        return self._generate(
            messages=messages,
            stop=stop,
            run_manager=run_manager,
        )

    async def agenerate_prompt(
        self,
        prompt: Union[ChatPromptValue, List[BaseMessage]],
        stop: Optional[List[str]] = None,
        run_manager=None,
        callbacks: Any = None,
        **kwargs: Any,
    ) -> LLMResult:
        if isinstance(prompt, list):
            messages = prompt
        else:
            messages = prompt.to_messages()

        return await self._agenerate(
            messages=messages,
            stop=stop,
            run_manager=run_manager,
        )
    
    # Простое текстовое предсказание
    def predict(self, text: str, stop: Optional[List[str]] = None) -> str:
        res = self._generate([HumanMessage(content=text)], stop=stop)
        return res.generations[0].text

    async def apredict(self, text: str, stop: Optional[List[str]] = None) -> str:
        res = await self._agenerate([HumanMessage(content=text)], stop=stop)
        return res.generations[0].text

    # Предсказание по списку BaseMessage
    def predict_messages(self, messages: List[BaseMessage], stop: Optional[List[str]] = None) -> BaseMessage:
        res = self._generate(messages, stop=stop)
        return res.generations[0].message

    async def apredict_messages(self, messages: List[BaseMessage], stop: Optional[List[str]] = None) -> BaseMessage:
        res = await self._agenerate(messages, stop=stop)
        return res.generations[0].message

    # Чтобы агенту можно было вызывать .invoke()
    def invoke(
        self,
        prompt: Union[str, BaseMessage, List[BaseMessage]],
        stop: Optional[List[str]] = None,
        sender: Optional[str] = None,
    ) -> ChatResult:
        # 1) Подготовить список сообщений
        if isinstance(prompt, str):
            msgs = [HumanMessage(content=prompt)]
        elif isinstance(prompt, BaseMessage):
            msgs = [prompt]
        else:
            msgs = prompt

        serial = [m.dict() for m in msgs]
        payload = {
            "method": "invoke",
            "args": [],
            "kwargs": {"messages": serial, "stop": stop},
            "sender": sender,
        }

        # 2) Отправить и разобрать LLMResult
        resp = self._client.post(self.endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
        data.pop("type", None)           # удаляем лишнее поле
        lm_res = LLMResult.parse_obj(data)

        # 3) Извлечь именно первый batch и вернуть flat список
        gens = lm_res.generations
        if gens and isinstance(gens[0], list):
            flat = gens[0]
        else:
            flat = gens  # уже flat

        return ChatResult(generations=flat, llm_output=lm_res.llm_output)


def get_llm() -> RemoteLLM:
    return RemoteLLM()