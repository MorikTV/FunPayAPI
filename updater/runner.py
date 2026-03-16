from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Generator

import requests

if TYPE_CHECKING:
    from ..account import Account

from ..common import exceptions, utils
from ..common.enums import OrderStatuses
from .. import types
from .events import (
    BaseEvent,
    InitialChatEvent, ChatsListChangedEvent, LastChatMessageChangedEvent,
    NewMessageEvent, MessageEventsStack,
    InitialOrderEvent, OrdersListChangedEvent, NewOrderEvent, OrderStatusChangedEvent,
)

logger = logging.getLogger("FunPayAPI.runner")

AnyEvent = (InitialChatEvent | ChatsListChangedEvent | LastChatMessageChangedEvent |
            NewMessageEvent | InitialOrderEvent | OrdersListChangedEvent |
            NewOrderEvent | OrderStatusChangedEvent)


class Runner:
    """
    Класс для получения новых событий FunPay.

    :param account: инициализированный экземпляр аккаунта.
    :param disable_message_requests: не запрашивать историю чатов (NewMessageEvent не будет).
    :param disabled_order_requests: не запрашивать список заказов.
    """

    def __init__(self, account: Account,
                 disable_message_requests: bool = False,
                 disabled_order_requests: bool = False):
        if not account.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if account.runner:
            raise RuntimeError("К данному аккаунту уже привязан Runner.")

        self.account: Account = account
        self.make_msg_requests: bool = not disable_message_requests
        self.make_order_requests: bool = not disabled_order_requests

        self.__last_msg_event_tag: str = utils.random_tag()
        self.__last_order_event_tag: str = utils.random_tag()
        self.__first_request: bool = True

        self.saved_orders: dict[str, types.OrderShortcut] | None = None
        self.last_messages_ids: dict[int, int] = {}
        self.chat_node_tags: dict[int, str] = {}
        self.users_ids: dict[int, int] = {}
        self.by_bot_ids: dict[int, list[int]] = {}
        self.runner_last_messages: dict[int, list] = {}

        account.runner = self

    # ══════════════════════════════════════════════════════════════════════════
    # Основные методы
    # ══════════════════════════════════════════════════════════════════════════

    def get_updates(self) -> dict:
        """Запрашивает события с FunPay runner/."""
        response = self.account.abuse_runner(
            last_msg_event_tag=self.__last_msg_event_tag,
            last_order_event_tag=self.__last_order_event_tag,
        )
        return response.json()

    def parse_updates(self, updates_objects: list[dict]) -> list[AnyEvent]:
        """Парсит объекты ответа runner/ и возвращает список событий."""
        events: list[AnyEvent] = []

        orders_counters_obj = None
        chat_bookmarks_obj = None

        for obj in updates_objects:
            t = obj.get("type")
            if t == "orders_counters":
                orders_counters_obj = obj
            elif t == "chat_bookmarks":
                chat_bookmarks_obj = obj

        # ── chat_bookmarks — список чатов ────────────────────────────────────
        if chat_bookmarks_obj:
            events.extend(self.__parse_chat_bookmarks(chat_bookmarks_obj))

        # ── orders_counters — список заказов ─────────────────────────────────
        if orders_counters_obj:
            events.extend(self.__parse_orders_counters(orders_counters_obj))

        if self.__first_request:
            self.__first_request = False

        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Парсинг чатов
    # ══════════════════════════════════════════════════════════════════════════

    def __parse_chat_bookmarks(self, obj: dict) -> list[AnyEvent]:
        events: list[AnyEvent] = []
        new_tag = obj.get("tag")

        if not obj.get("data"):
            self.__last_msg_event_tag = new_tag
            return events

        self.__last_msg_event_tag = new_tag
        chat_html = obj["data"].get("html", "")

        from bs4 import BeautifulSoup
        parser = BeautifulSoup(chat_html, "lxml")
        chats_found = parser.find_all("a", {"class": "contact-item"})

        changed_chats: list[types.ChatShortcut] = []

        for msg in chats_found:
            chat_id = int(msg["data-id"])
            last_msg_text = msg.find("div", {"class": "contact-item-message"}).text
            unread = "unread" in msg.get("class", [])
            chat_with = msg.find("div", {"class": "media-user-name"}).text
            node_msg_id = int(msg.get("data-node-msg"))
            user_msg_id = int(msg.get("data-user-msg"))

            by_bot = by_vertex = False
            is_image = last_msg_text in ("Изображение", "Зображення", "Image")
            if last_msg_text.startswith(self.account.bot_character):
                last_msg_text, by_bot = last_msg_text[1:], True
            elif last_msg_text.startswith(self.account.old_bot_character):
                last_msg_text, by_vertex = last_msg_text[1:], True

            chat_obj = types.ChatShortcut(chat_id, chat_with, last_msg_text,
                                          node_msg_id, user_msg_id, unread, str(msg))
            if not is_image:
                chat_obj.last_by_bot = by_bot
                chat_obj.last_by_vertex = by_vertex

            prev = self.account._Account__saved_chats.get(chat_id)  # noqa

            if self.__first_request:
                events.append(InitialChatEvent(self.__last_msg_event_tag, chat_obj))
            elif prev is None or prev.node_msg_id != node_msg_id:
                events.append(LastChatMessageChangedEvent(self.__last_msg_event_tag, chat_obj))
                changed_chats.append(chat_obj)

            self.account.add_chats([chat_obj])

        if changed_chats and not self.__first_request:
            events.append(ChatsListChangedEvent(self.__last_msg_event_tag))

        # ── Запрашиваем историю изменившихся чатов ───────────────────────────
        if self.make_msg_requests and changed_chats:
            events.extend(self.__get_new_messages(changed_chats))

        return events

    def __get_new_messages(self, chats: list[types.ChatShortcut]) -> list[NewMessageEvent]:
        """Запрашивает историю переданных чатов и возвращает NewMessageEvent-ы."""
        events: list[NewMessageEvent] = []

        chats_data = {chat.id: chat.name for chat in chats}

        try:
            histories = self.account.get_chats_histories(chats_data)
        except Exception as e:
            logger.error(f"Не удалось получить историю чатов: {e}")
            logger.debug("TRACEBACK", exc_info=True)
            return events

        for chat_id, messages in histories.items():
            if not messages:
                continue

            last_known_id = self.last_messages_ids.get(chat_id, 0)

            # Оставляем только новые сообщения
            new_messages = [m for m in messages if m.id > last_known_id]
            if not new_messages:
                # Первый раз видим этот чат — берём только последнее
                new_messages = messages[-1:]

            # Помечаем сообщения отправленные ботом
            if self.by_bot_ids.get(chat_id):
                for m in new_messages:
                    if not m.by_bot and m.id in self.by_bot_ids[chat_id]:
                        m.by_bot = True

            self.last_messages_ids[chat_id] = new_messages[-1].id
            if new_messages[-1].tag:
                self.chat_node_tags[chat_id] = new_messages[-1].tag
            if new_messages[-1].interlocutor_id:
                self.users_ids[chat_id] = new_messages[-1].interlocutor_id
            self.by_bot_ids[chat_id] = [i for i in self.by_bot_ids.get(chat_id, [])
                                         if i > self.last_messages_ids[chat_id]]

            stack = MessageEventsStack()
            for msg in new_messages:
                event = NewMessageEvent(self.__last_msg_event_tag, msg, stack)
                stack.add_events([event])
                events.append(event)

        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Парсинг заказов
    # ══════════════════════════════════════════════════════════════════════════

    def __parse_orders_counters(self, obj: dict) -> list[AnyEvent]:
        events: list[AnyEvent] = []
        self.__last_order_event_tag = obj.get("tag")

        if not self.__first_request:
            events.append(OrdersListChangedEvent(
                self.__last_order_event_tag,
                obj["data"]["buyer"],
                obj["data"]["seller"],
            ))

        if not self.make_order_requests:
            return events

        attempts = 3
        orders_list = None
        while attempts:
            attempts -= 1
            try:
                _, orders_list, *_ = self.account.get_sales()
                break
            except exceptions.RequestFailedError as e:
                logger.error(e)
            except Exception:
                logger.error("Не удалось обновить список заказов.")
                logger.debug("TRACEBACK", exc_info=True)
            time.sleep(1)

        if orders_list is None:
            logger.error("Не удалось обновить список продаж: превышено кол-во попыток.")
            return events

        now_orders: dict[str, types.OrderShortcut] = {}
        for order in orders_list:
            now_orders[order.id] = order
            if self.saved_orders is None:
                events.append(InitialOrderEvent(self.__last_order_event_tag, order))
            elif order.id not in self.saved_orders:
                events.append(NewOrderEvent(self.__last_order_event_tag, order))
                if order.status == OrderStatuses.CLOSED:
                    events.append(OrderStatusChangedEvent(self.__last_order_event_tag, order))
            elif order.status != self.saved_orders[order.id].status:
                events.append(OrderStatusChangedEvent(self.__last_order_event_tag, order))
        self.saved_orders = now_orders
        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Вспомогательные методы
    # ══════════════════════════════════════════════════════════════════════════

    def update_last_message(self, chat_id: int, message_id: int,
                            message_text: str | None):
        """Обновляет кэш последнего сообщения чата."""
        self.runner_last_messages[chat_id] = [message_id, message_id, message_text]
        self.last_messages_ids[chat_id] = message_id

    def mark_as_by_bot(self, chat_id: int, message_id: int):
        """Помечает сообщение как отправленное ботом."""
        self.by_bot_ids.setdefault(chat_id, []).append(message_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Генератор событий
    # ══════════════════════════════════════════════════════════════════════════

    def listen(self, requests_delay: int | float = 6.0,
               ignore_exceptions: bool = True) -> Generator[AnyEvent, None, None]:
        """
        Бесконечно опрашивает FunPay и генерирует события.

        :param requests_delay: пауза между запросами (секунды).
        :param ignore_exceptions: не бросать исключения, только логировать.
        """
        while True:
            start_time = time.time()
            try:
                updates = self.get_updates()
                for event in self.parse_updates(updates["objects"]):
                    yield event
            except Exception as e:
                if not ignore_exceptions:
                    raise e
                logger.error(f"Ошибка при получении событий: {e}")
                logger.debug("TRACEBACK", exc_info=True)

            elapsed = time.time() - start_time
            sleep_time = requests_delay - elapsed
            if time.time() - self.account.last_429_err_time > 60:
                if sleep_time > 0:
                    time.sleep(sleep_time)
            else:
                time.sleep(requests_delay)