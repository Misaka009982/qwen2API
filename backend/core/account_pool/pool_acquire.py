"""
账号获取逻辑 - 对齐 ds2api 的 pool_acquire.go
"""
import asyncio
import logging
import random
import time
from typing import Optional, TYPE_CHECKING

from backend.core.config import settings

if TYPE_CHECKING:
    from backend.core.account_pool.pool_core import Account

log = logging.getLogger("qwen2api.accounts.acquire")


def _jitter_seconds() -> float:
    """随机抖动"""
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0


class AccountAcquireMixin:
    """账号获取逻辑混入类"""

    async def _remove_waiter(self, waiter: asyncio.Event) -> None:
        """
        从等待队列中移除 waiter（若仍在队列中）。

        acquire_wait* 可能因超时返回；若不清理 waiter，队列长度会持续增长，
        在 max_queue_size 较小时会出现“队列假满”并拒绝后续请求。
        """
        async with self._lock:
            queue = getattr(self._waiters_queue, "_queue", None)
            if queue is None:
                return
            try:
                queue.remove(waiter)
            except ValueError:
                # 已被 release() 的 notify 弹出，属于正常竞争场景
                pass

    async def acquire(self, exclude: Optional[set] = None) -> Optional["Account"]:
        """
        立即获取账号（不等待）
        对齐 ds2api 的 Acquire() 逻辑
        """
        async with self._lock:
            now = time.time()

            # 检查全局并发限制
            if not self._can_acquire_global():
                return None

            # 筛选可用账号
            available = [a for a in self.accounts if a.is_available() and (not exclude or a.email not in exclude)]
            if not available:
                return None

            # 筛选就绪账号（未达到并发上限且冷却完成）
            ready = [a for a in available if a.inflight < self.max_inflight_per_account and a.next_available_at() <= now]
            if not ready:
                return None

            # 按负载排序：优先选择 inflight 最少的账号
            ready.sort(key=lambda a: (a.inflight, a.last_request_started or 0.0, a.last_used or 0.0))
            best = ready[0]

            # 分配账号
            best.inflight += 1
            best.last_used = now
            best.last_request_started = now + _jitter_seconds()
            self.global_in_use += 1
            self._sticky_email = best.email if len(ready) == 1 else None

            return best

    async def acquire_preferred(self, preferred_email: Optional[str] = None, exclude: Optional[set] = None) -> Optional["Account"]:
        """
        优先获取指定账号
        对齐 ds2api 的 AcquirePreferred() 逻辑
        """
        if not preferred_email:
            return await self.acquire(exclude)

        async with self._lock:
            now = time.time()

            # 检查全局并发限制
            if not self._can_acquire_global():
                return None

            # 查找指定账号
            preferred = next((a for a in self.accounts if a.email == preferred_email), None)
            if (
                preferred
                and preferred.is_available()
                and preferred.inflight < self.max_inflight_per_account
                and preferred.next_available_at() <= now
                and (not exclude or preferred.email not in exclude)
            ):
                preferred.inflight += 1
                preferred.last_used = now
                preferred.last_request_started = now + _jitter_seconds()
                self.global_in_use += 1
                self._sticky_email = preferred.email
                return preferred

        # 指定账号不可用，回退到普通获取
        return await self.acquire(exclude)

    async def acquire_wait(self, timeout: float = 60, exclude: Optional[set] = None) -> Optional["Account"]:
        """
        等待获取账号（带超时）
        对齐 ds2api 的 AcquireWait() 逻辑
        """
        deadline = time.time() + timeout

        while True:
            # 尝试立即获取
            acc = await self.acquire(exclude)
            if acc:
                return acc

            # 检查是否还有候选账号
            async with self._lock:
                candidates = [a for a in self.accounts if a.valid and (not exclude or a.email not in exclude)]
                if not candidates:
                    return None

                # 计算下次可用时间
                next_ready_at = min((a.next_available_at() for a in candidates), default=time.time())

            # 检查超时
            remaining = deadline - time.time()
            if remaining <= 0:
                return None

            # 检查队列是否已满
            if not self._can_queue():
                log.warning(f"[AccountPool] 等待队列已满 ({self._waiters_queue.qsize()}/{self.max_queue_size})")
                return None

            # 加入等待队列
            waiter = asyncio.Event()
            await self._waiters_queue.put(waiter)

            # 计算等待时间
