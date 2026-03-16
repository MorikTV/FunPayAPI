from __future__ import annotations
import time
from ..common import utils
from ..common.enums import EventTypes
from .. import types


class BaseEvent:
    """
    Базовый класс события.

    :param runner_tag: тег Runner'а.
    :param event_type: тип события.
    :param event_time: время события (генерируется автоматически, если не указано).
    """

    def __init__(self, runner_tag: str, event_type: EventTypes,
                 event_time: int | float | None = None):
        self.runner_tag: str = runner_tag
        self.type: EventTypes = event_type
        self.time: float = event_time if event_time is not None else time.time()


# ──────────────────────────────────────────────────────────────────────────────
# События чатов
# ──────────────────────────────────────────────────────────────────────────────

class InitialChatEvent(BaseEvent):
    """
    Событие: обнаружен чат при первом запросе Runner'а.

    :param runner_tag: тег Runner'а.
    :param chat_obj: объект обнаруженного чата.
    """

    def __init__(self, runner_tag: str, chat_obj: types.ChatShortcut):
        super().__init__(runner_tag, EventTypes.INITIAL_CHAT)
        self.chat: types.ChatShortcut = chat_obj


class ChatsListChangedEvent(BaseEvent):
    """
    Событие: список чатов и/или содержимое одного/нескольких чатов изменилось.

    :param runner_tag: тег Runner'а.
    """

    def __init__(self, runner_tag: str):
        super().__init__(runner_tag, EventTypes.CHATS_LIST_CHANGED)


class LastChatMessageChangedEvent(BaseEvent):
    """
    Событие: последнее сообщение в чате изменилось.

    :param runner_tag: тег Runner'а.
    :param chat_obj: объект чата, в котором изменилось последнее сообщение.
    """

    def __init__(self, runner_tag: str, chat_obj: types.ChatShortcut):
        super().__init__(runner_tag, EventTypes.LAST_CHAT_MESSAGE_CHANGED)
        self.chat: types.ChatShortcut = chat_obj


class MessageEventsStack:
    """
    Стэк событий новых сообщений из одного чата за один запрос Runner'а.
    Позволяет получить доступ ко всем новым сообщениям от одного пользователя сразу.
    """

    def __init__(self):
        self.__id: str = utils.random_tag()
        self.__stack: list[NewMessageEvent] = []

    def add_events(self, messages: list[NewMessageEvent]):
        """Добавляет события в стэк."""
        self.__stack.extend(messages)

    def get_stack(self) -> list[NewMessageEvent]:
        """Возвращает все события стэка."""
        return self.__stack

    def id(self) -> str:
        """Возвращает случайный ID стэка."""
        return self.__id


class NewMessageEvent(BaseEvent):
    """
    Событие: в истории чата обнаружено новое сообщение.

    :param runner_tag: тег Runner'а.
    :param message_obj: объект нового сообщения.
    :param stack: стэк событий новых сообщений из этого чата (опционально).
    """

    def __init__(self, runner_tag: str, message_obj: types.Message,
                 stack: MessageEventsStack | None = None):
        super().__init__(runner_tag, EventTypes.NEW_MESSAGE)
        self.message: types.Message = message_obj
        self.stack: MessageEventsStack | None = stack


# ──────────────────────────────────────────────────────────────────────────────
# События заказов
# ──────────────────────────────────────────────────────────────────────────────

class InitialOrderEvent(BaseEvent):
    """
    Событие: обнаружен заказ при первом запросе Runner'а.

    :param runner_tag: тег Runner'а.
    :param order_obj: объект обнаруженного заказа.
    """

    def __init__(self, runner_tag: str, order_obj: types.OrderShortcut):
        super().__init__(runner_tag, EventTypes.INITIAL_ORDER)
        self.order: types.OrderShortcut = order_obj


class OrdersListChangedEvent(BaseEvent):
    """
    Событие: список заказов и/или статус одного/нескольких заказов изменился.

    :param runner_tag: тег Runner'а.
    :param purchases: кол-во незавершённых покупок.
    :param sales: кол-во незавершённых продаж.
    """

    def __init__(self, runner_tag: str, purchases: int, sales: int):
        super().__init__(runner_tag, EventTypes.ORDERS_LIST_CHANGED)
        self.purchases: int = purchases
        self.sales: int = sales


class NewOrderEvent(BaseEvent):
    """
    Событие: в списке заказов обнаружен новый заказ.

    :param runner_tag: тег Runner'а.
    :param order_obj: объект нового заказа.
    """

    def __init__(self, runner_tag: str, order_obj: types.OrderShortcut):
        super().__init__(runner_tag, EventTypes.NEW_ORDER)
        self.order: types.OrderShortcut = order_obj


class OrderStatusChangedEvent(BaseEvent):
    """
    Событие: статус заказа изменился.

    :param runner_tag: тег Runner'а.
    :param order_obj: объект заказа с новым статусом.
    """

    def __init__(self, runner_tag: str, order_obj: types.OrderShortcut):
        super().__init__(runner_tag, EventTypes.ORDER_STATUS_CHANGED)
        self.order: types.OrderShortcut = order_obj