"""
FunPayAPI — неофициальная Python-библиотека для взаимодействия с FunPay.

Быстрый старт::

    from FunPayAPI import Account
    from FunPayAPI.updater.runner import Runner
    from FunPayAPI.updater.events import NewMessageEvent

    account = Account(golden_key="ВАШ_КЛЮЧ")
    account.get()

    runner = Runner(account)
    for event in runner.listen():
        if isinstance(event, NewMessageEvent):
            print(f"[{event.message.author}]: {event.message.text}")
"""

from .account import Account
from . import types
from .common import enums, exceptions, utils
from .common.enums import (
    EventTypes,
    MessageTypes,
    OrderStatuses,
    SubCategoryTypes,
    Currency,
    Wallet,
)

__all__ = [
    "Account",
    "types",
    "enums",
    "exceptions",
    "utils",
    "EventTypes",
    "MessageTypes",
    "OrderStatuses",
    "SubCategoryTypes",
    "Currency",
    "Wallet",
]