import asyncio
from typing import Any, List, Callable, Any, TypeVar
from functools import partial
from functools import wraps
from sanic.log import logger
import time

T = TypeVar("T")


def async_perf_count(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        instance_name: str = ''
        if args:
            self_obj = args[0]
            instance_name = getattr(self_obj, 'name', self_obj.__class__.__name__)
        start = time.perf_counter_ns()
        func_name = func.__name__
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            print(e)
        finally:
            cost = time.perf_counter_ns() - start
            logger.info(f"{instance_name} {func_name} takes {cost/1e6} ms to execute")

    return wrapper


class Promise:
    def __init__(
        self,
    ) -> None:
        self._ctx = None
        self._chains = []

    @property
    def ctx(self):
        return self._ctx

    @ctx.setter
    def ctx(self, value):
        self._ctx = value

    def then(self, *funcs: Callable) -> "Promise":
        self._chains.append(funcs)
        return self

    async def async_run_with_wraps(self, func: Callable):
        if asyncio.iscoroutinefunction(func):
            await func(self.ctx)
        else:
            await run_in_async(func, self.ctx)

    async def execute(self, ctx: Any):
        self.ctx = ctx
        for chain in self._chains:
            if len(chain) == 1:
                func = chain[0]
                await self.async_run_with_wraps(func)
            else:
                tasks = [self.async_run_with_wraps(func) for func in chain]
                await asyncio.gather(*tasks)

        return self.ctx


async def run_in_async(func: Callable[..., T], *args: Any, **kwargs: Any):
    loop = asyncio.get_running_loop()

    if kwargs:
        func_call = partial(func, *args, **kwargs)
        return await loop.run_in_executor(None, func_call)
    else:
        return await loop.run_in_executor(None, func, *args)
