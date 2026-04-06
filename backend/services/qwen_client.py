import asyncio
import json
import logging
import time
import uuid
from typing import Optional
from backend.core.browser_engine import BrowserEngine
from backend.core.account_pool import AccountPool, Account
from backend.core.config import settings
from backend.services.auth_resolver import AuthResolver

log = logging.getLogger("qwen2api.client")

class QwenClient:
    def __init__(self, engine: BrowserEngine, account_pool: AccountPool):
        self.engine = engine
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool)

    async def create_chat(self, token: str, model: str) -> str:
        ts = int(time.time())
        body = {"title": f"api_{ts}", "models": [model], "chat_mode": "normal",
                "chat_type": "t2t", "timestamp": ts}
        
        r = await self.engine.api_call("POST", "/api/v2/chats/new", token, body)
        if r["status"] == 429:
            raise Exception("429 Too Many Requests (Engine Queue Full)")
            
        body_text = r.get("body", "")
        if r["status"] != 200:
            raise Exception(f"create_chat HTTP {r['status']}: {body_text[:100]}")
            
        try:
            data = json.loads(body_text)
            return data["data"]["id"]
        except Exception as e:
            raise Exception(f"create_chat parse error: {e}, body={body_text[:200]}")

    async def delete_chat(self, token: str, chat_id: str):
        await self.engine.api_call("DELETE", f"/api/v2/chats/{chat_id}", token)

    async def verify_account(self, acc: Account) -> bool:
        """Verify token validity via direct HTTP (no browser page needed)."""
        if not acc.token:
            return False
            
        try:
            import httpx
            from backend.services.auth_resolver import BASE_URL
            
            headers = {
                "Authorization": f"Bearer {acc.token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*"
            }
            # 如果有 cookies，也一并带上
            if acc.cookies:
                headers["Cookie"] = acc.cookies

            async with httpx.AsyncClient(timeout=15, trust_env=False) as hc:
                resp = await hc.get(
                    f"{BASE_URL}/api/v1/auths/",
                    headers=headers,
                )
                
            if resp.status_code != 200:
                log.warning(f"[Verify] Account {acc.email} HTTP {resp.status_code}")
                return False
                
            try:
                data = resp.json()
            except Exception as json_err:
                log.warning(f"[Verify] Account {acc.email} returned non-JSON response (status {resp.status_code}): {resp.text[:100]}... Error: {json_err}")
                return False
                
            is_valid = data.get("role") == "user"
            
            if not is_valid:
                log.warning(f"[Verify] Account {acc.email} is NOT a valid user. Data: {data}")
                
            return is_valid
        except Exception as e:
            log.warning(f"[Verify] Account {acc.email} HTTP error: {e}")
            return False

    def _build_payload(self, chat_id: str, model: str, content: str) -> dict:
        ts = int(time.time())
        return {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chat_id": chat_id, "chat_mode": "normal", "model": model, "parent_id": None,
            "messages": [{
                "fid": str(uuid.uuid4()), "parentId": None, "childrenIds": [str(uuid.uuid4())],
                "role": "user", "content": content, "user_action": "chat", "files": [],
                "timestamp": ts, "models": [model], "chat_type": "t2t",
                "feature_config": {"thinking_enabled": False, "auto_search": False, "code_interpreter": False},
                "extra": {"meta": {"subChatType": "t2t"}}, "sub_chat_type": "t2t", "parent_id": None,
            }],
            "timestamp": ts,
        }

    def parse_sse_body(self, body: str) -> list[dict]:
        events = []
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                events.append({"type": "done"})
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
                
            if "code" in data and "message" in data and "choices" not in data:
                raise Exception(f"Qwen API error {data.get('code')}: {data.get('message')}")
                
            choices = data.get("choices", [])
            if not choices:
                continue
                
            delta = choices[0].get("delta", {})
            events.append({
                "type": "delta",
                "phase": delta.get("phase", ""),
                "content": delta.get("content", ""),
                "status": delta.get("status", "")
            })
        return events

    async def chat_stream_events_with_retry(self, model: str, content: str) -> tuple[list[dict], str, Account]:
        """无感容灾重试逻辑：上游挂了自动换号"""
        exclude = set()
        for attempt in range(settings.MAX_RETRIES):
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            if not acc:
                raise Exception("No available accounts in pool (all busy or rate limited)")
                
            try:
                chat_id = await self.create_chat(acc.token, model)
                payload = self._build_payload(chat_id, model, content)
                result = await self.engine.fetch_chat(acc.token, chat_id, payload)
                
                if result["status"] == 429:
                    raise Exception("Engine Queue Full")
                    
                if result["status"] != 200:
                    raise Exception(f"HTTP {result['status']}: {result['body'][:100]}")
                    
                events = self.parse_sse_body(result["body"])
                return events, chat_id, acc
                
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    self.account_pool.mark_rate_limited(acc)
                    exclude.add(acc.email)
                elif "unauthorized" in err_msg or "401" in err_msg or "403" in err_msg:
                    self.account_pool.mark_invalid(acc)
                    exclude.add(acc.email)
                    # 触发自愈
                    asyncio.create_task(self.auth_resolver.refresh_token(acc))
                else:
                    # 瞬时错误，不标记死号，但排除它并重试下一个
                    exclude.add(acc.email)
                
                self.account_pool.release(acc)
                log.warning(f"[Retry {attempt+1}/{settings.MAX_RETRIES}] Account {acc.email} failed: {e}. Retrying...")
                
        raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Please check upstream accounts.")
