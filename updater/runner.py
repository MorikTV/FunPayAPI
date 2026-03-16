from __future__ import annotations

import logging
import random
import time
import uuid
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

# Тип возвращаемого события для аннотаций
AnyEvent = (InitialChatEvent | ChatsListChangedEvent | LastChatMessageChangedEvent |
            NewMessageEvent | InitialOrderEvent | OrdersListChangedEvent |
            NewOrderEvent | OrderStatusChangedEvent)


class Runner:
    """
    Класс для получения новых событий FunPay.

    Работает в двух режимах:
    - **Standalone** (``loop()`` не запущен): запросы идут напрямую через ``Account.runner_request()``.
    - **Async-batching** (``loop()`` запущен в отдельном потоке): запросы батчируются
      через ``payload_queue`` / ``runner_results`` для экономии квоты runner-эндпоинта.

    :param account: инициализированный экземпляр аккаунта.
    :param disable_message_requests: не делать доп. запросы для получения новых сообщений.
        Если ``True`` — события :class:`NewMessageEvent` не генерируются.
    :param disabled_order_requests: не делать доп. запросы для получения заказов.
        Если ``True`` — события :class:`InitialOrderEvent`, :class:`NewOrderEvent`,
        :class:`OrderStatusChangedEvent` не генерируются.
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

        # ── Теги событий ──────────────────────────────────────────────────────
        self.__last_msg_event_tag: str = utils.random_tag()
        self.__last_order_event_tag: str = utils.random_tag()
        self.__first_request: bool = True
        self.__is_running: bool = False

        # ── Кэш состояния ─────────────────────────────────────────────────────
        self.saved_orders: dict[str, types.OrderShortcut] | None = None
        """Сохранённые состояния заказов: {ID заказа: OrderShortcut}."""

        self.runner_last_messages: dict[int, list] = {}
        """ID последних сообщений: {ID чата: [node_id, user_id, text | None]}."""

        self.by_bot_ids: dict[int, list[int]] = {}
        """ID сообщений, отправленных ботом: {ID чата: [message_id, ...]}."""

        self.last_messages_ids: dict[int, int] = {}
        """ID последнего сообщения в чате: {ID чата: message_id}."""

        self.chat_node_tags: dict[int, str] = {}
        """Теги узлов чатов: {ID чата: tag}."""

        self.users_ids: dict[int, int] = {}
        """Маппинг ID чата → ID собеседника."""

        self.buyers_viewing: dict[int, types.BuyerViewing] = {}
        """Что смотрит покупатель: {ID покупателя: BuyerViewing}."""

        self.runner_len: int = 10
        """Максимальное кол-во объектов в одном runner-запросе."""

        # ── Async-batching (используется вместе с loop()) ─────────────────────
        self.payload_queue: dict[str, dict] = {}
        """Очередь ожидающих выполнения payload-ов: {uuid: payload}."""

        self.runner_results: dict[str, requests.Response | Exception] = {}
        """Результаты выполненных запросов: {uuid: Response | Exception}."""

        # ── Внутренние буферы для listen() ───────────────────────────────────
        self.__orders_counters: dict | None = None
        self.__chat_bookmarks: list[dict] = []
        self.__chat_bookmarks_time: float = 0
        self.__chat_nodes: dict[int, tuple[dict, int]] = {}

        account.runner = self

    # ══════════════════════════════════════════════════════════════════════════
    # Async-batching
    # ══════════════════════════════════════════════════════════════════════════

    def __add_payload(self, payload: dict) -> str:
        """Добавляет payload в очередь и возвращает его UUID."""
        id_ = str(uuid.uuid4())
        self.payload_queue[id_] = payload
        return id_

    def get_result(self, payload: dict) -> requests.Response:
        """
        Добавляет payload в очередь и блокирует вызывающий поток до получения ответа.
        Используется когда ``loop()`` запущен в отдельном потоке.

        :param payload: словарь с данными для runner/.
        :return: объект ответа.
        :raises Exception: если ответ не получен или запрос завершился ошибкой.
        """
        id_ = self.__add_payload(payload)
        # Ждём, пока payload не будет извлечён из очереди (loop обработает его)
        while id_ in self.payload_queue:
            time.sleep(0.1)
        # Ждём результата
        for _ in range(300):
            if id_ in self.runner_results:
                break
            time.sleep(0.1)
        result = self.runner_results.pop(id_, Exception("Не удалось получить результат от Runner."))
        if isinstance(result, Exception):
            raise result
        return result

    def loop(self):
        """
        Основной цикл батч-обработки payload-ов.
        Предназначен для запуска в **отдельном потоке**.
        Объединяет несколько ожидающих запросов в один runner-запрос
        (экономит лимиты FunPay).

        Пример использования::

            import threading
            runner = Runner(account)
            t = threading.Thread(target=runner.loop, daemon=True)
            t.start()
            for event in runner.listen():
                ...
        """
        if self.__is_running:
            return
        self.__is_running = True

        while True:
            try:
                request_data: dict = {"objects": [], "request": False}
                ids: set[str] = set()

                for id_ in list(self.payload_queue.keys()):
                    payload = self.payload_queue.get(id_)
                    if payload is None:
                        continue
                    can_merge = (
                        (not request_data["objects"] and not request_data["request"])
                        or (len(request_data["objects"]) + len(payload["objects"]) <= self.runner_len
                            and int(bool(request_data["request"])) + int(bool(payload["request"])) <= 1)
                    )
                    if can_merge:
                        request_data["objects"].extend(payload["objects"])
                        request_data["request"] = request_data["request"] or payload["request"]
                        ids.add(id_)
                        self.payload_queue.pop(id_, None)
                    else:
                        break

                if not request_data["objects"] and not request_data["request"]:
                    time.sleep(0.1)
                    continue

                types_ = [o["type"] for o in request_data["objects"]]
                is_listener_request = ("orders_counters" in types_ and "chat_bookmarks" in types_)
                request_data = self.__fill_request_data(request_data)

                try:
                    result = self.account.runner_request(request_data)
                except Exception as e:
                    result = e

                for id_ in ids:
                    self.runner_results[id_] = result

                if isinstance(result, Exception):
                    time.sleep(5)
                    continue

                try:
                    parsed = result.json()
                    for obj in parsed["objects"]:
                        if not is_listener_request and obj["type"] == "orders_counters":
                            self.__orders_counters = obj
                        elif (obj["type"] == "chat_bookmarks"
                              and (data := obj.get("data")) and data.get("order")):
                            if not is_listener_request:
                                self.__chat_bookmarks.append(obj)
                        elif (self.make_msg_requests
                              and obj["type"] == "chat_node"
                              and (data := obj.get("data"))
                              and (node := data.get("node"))
                              and (node_id := node.get("id"))
                              and (messages := data.get("messages"))):
                            last_msg_id = messages[-1]["id"]
                            if (last_msg_id > self.last_messages_ids.get(node_id, 0)
                                    and (node_id not in self.__chat_nodes
                                         or last_msg_id > self.__chat_nodes[node_id][1])):
                                self.__chat_nodes[node_id] = (obj, last_msg_id)
                except Exception:
                    logger.warning("Ошибка при разборе ответа Runner.")
                    logger.debug("TRACEBACK", exc_info=True)

            except Exception:
                logger.error("Необработанная ошибка в loop().")
                logger.debug("TRACEBACK", exc_info=True)

    def __fill_request_data(self, request_data: dict) -> dict:
        """
        Дополняет запрос объектами ``chat_bookmarks`` / ``orders_counters`` / ``chat_node``
        в зависимости от текущего состояния runner'а.
        """
        if not self.__first_request:
            types_ = [o["type"] for o in request_data["objects"]]
            if (len(request_data["objects"]) < self.runner_len
                    and not self.__orders_counters
                    and "orders_counters" not in types_):
                request_data["objects"].extend(
                    self.account.get_payload_data(
                        last_order_event_tag=self.__last_order_event_tag)["objects"])

            if (len(request_data["objects"]) < self.runner_len
                    and time.time() - self.__chat_bookmarks_time > 1.5 ** len(self.__chat_bookmarks) - 1
                    and "chat_bookmarks" not in [o["type"] for o in request_data["objects"]]):
                request_data["objects"].extend(
                    self.account.get_payload_data(
                        last_msg_event_tag=self.__last_msg_event_tag)["objects"])
                self.__chat_bookmarks_time = time.time()

        try:
            if self.make_msg_requests and (remaining := self.runner_len - len(request_data["objects"])) > 0:
                active_chats = self.__detect_chats_with_activity(remaining)
                payload_data = self.account.get_payload_data(chats_data=active_chats,
                                                              include_runner_context=True)
                request_data["objects"].extend(payload_data["objects"])
        except Exception:
            logger.warning("Ошибка при добавлении активных чатов в запрос.")
            logger.debug("TRACEBACK", exc_info=True)
        return request_data

    def __detect_chats_with_activity(self, amount: int) -> list[int]:
        """
        Определяет наиболее активные чаты на основе истории bookmarks.
        Используется для проактивного опроса сообщений.
        """
        if not self.__chat_bookmarks or len(self.__chat_bookmarks) < 2:
            return []
        new_list = self.__chat_bookmarks[-1]["data"]["order"]
        old_list = random.choice(self.__chat_bookmarks[:-1])["data"]["order"]
        old_positions = {chat_id: i for i, chat_id in enumerate(old_list)}

        last = float("inf")
        split_index = len(new_list)
        for i in range(len(new_list) - 1, -1, -1):
            idx = old_positions.get(new_list[i])
            if idx is None or i < idx or last < idx:
                split_index = i
                break
            else:
                last = idx

        result = new_list[:split_index + 1]
        if len(result) >= amount:
            return random.sample(result, amount)
        result = set(result)
        i = 0
        while len(result) < amount and i < len(new_list):
            result.add(new_list[i])
            i += 1
        return list(result)

    # ══════════════════════════════════════════════════════════════════════════
    # Получение и парсинг событий
    # ══════════════════════════════════════════════════════════════════════════

    def get_updates(self) -> dict:
        """
        Выполняет запрос к runner/ и возвращает необработанный JSON.

        :return: словарь с ключом ``objects``.
        """
        response = self.account.abuse_runner(
            last_msg_event_tag=self.__last_msg_event_tag,
            last_order_event_tag=self.__last_order_event_tag,
        )
        return response.json()

    def parse_updates(self, updates_objects: list[dict]) -> list[AnyEvent]:
        """
        Парсит список объектов из ответа runner/ и возвращает список событий.

        :param updates_objects: список объектов ``response["objects"]``.
        :return: список событий.
        """
        events: list[AnyEvent] = []

        # Сортируем: сначала chat_bookmarks/orders_counters, потом chat_node
        orders_counters_obj = None
        chat_bookmarks_objs = []
        chat_node_objs = []
        buyer_viewing_objs = []

        for obj in updates_objects:
            t = obj.get("type")
            if t == "orders_counters":
                orders_counters_obj = obj
            elif t == "chat_bookmarks":
                chat_bookmarks_objs.append(obj)
            elif t == "chat_node":
                chat_node_objs.append(obj)
            elif t == "c-p-u":
                buyer_viewing_objs.append(obj)

        # ── Обработка chat_bookmarks ─────────────────────────────────────────
        for obj in chat_bookmarks_objs:
            new_tag = obj.get("tag")
            if not obj.get("data"):
                self.__last_msg_event_tag = new_tag
                continue
            self.__last_msg_event_tag = new_tag
            chat_html = obj["data"].get("html", "")
            from bs4 import BeautifulSoup
            parser = BeautifulSoup(chat_html, "lxml")
            chats_found = parser.find_all("a", {"class": "contact-item"})

            is_changed = False
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
                elif prev is None or prev.node_msg_id != node_msg_id or prev.last_message_text != last_msg_text:
                    is_changed = True
                    events.append(LastChatMessageChangedEvent(self.__last_msg_event_tag, chat_obj))
                self.account.add_chats([chat_obj])

            if is_changed and not self.__first_request:
                events.append(ChatsListChangedEvent(self.__last_msg_event_tag))

        # ── Обработка orders_counters ────────────────────────────────────────
        if orders_counters_obj:
            events.extend(self.parse_order_updates(orders_counters_obj))

        # ── Обработка chat_node (новые сообщения) ────────────────────────────
        if self.make_msg_requests:
            # Объединяем с уже накопленными в loop()
            all_nodes: dict[int, dict] = {}
            for node_id, (obj, _) in self.__chat_nodes.items():
                all_nodes[node_id] = obj
            for obj in chat_node_objs:
                if not obj.get("data"):
                    continue
                node = obj["data"].get("node")
                if not node:
                    continue
                node_id = node.get("id")
                if node_id:
                    all_nodes[node_id] = obj

            msg_events = self.__parse_chat_node_events(all_nodes)
            for chat_events in msg_events.values():
                events.extend(chat_events)

        # ── Обработка buyer_viewing ──────────────────────────────────────────
        for obj in buyer_viewing_objs:
            bv = self.account._Account__parse_buyer_viewing(obj)  # noqa
            self.buyers_viewing[bv.buyer_id] = bv

        if self.__first_request:
            self.__first_request = False
        return events

    def __parse_chat_node_events(self, nodes: dict[int, dict]) -> dict[int, list[NewMessageEvent]]:
        """Парсит узлы чатов и генерирует NewMessageEvent-ы."""
        result: dict[int, list[NewMessageEvent]] = {}

        for node_id, obj in nodes.items():
            if not obj.get("data"):
                continue
            node = obj["data"].get("node")
            if not node:
                continue
            messages_raw = obj["data"].get("messages")
            if not messages_raw:
                continue

            cid = node["id"]
            tag = obj.get("tag")
            if node["silent"]:
                interlocutor_id = None
                interlocutor_name = None
            else:
                interlocutors = node["name"].split("-")[1:]
                interlocutors.remove(str(self.account.id))
                interlocutor_id = int(interlocutors[0])
                interlocutor_name = None
                if cs := self.account.get_chat_by_id(cid):
                    interlocutor_name = cs.name

            from_id = self.last_messages_ids.get(cid, 0)
            messages = self.account._Account__parse_messages(  # noqa
                messages_raw, cid, interlocutor_id, interlocutor_name,
                from_id=from_id + 1, is_private=not node["silent"], tag=tag,
            )
            if not messages:
                continue

            # Помечаем сообщения, отправленные ботом
            if self.by_bot_ids.get(cid):
                for m in messages:
                    if not m.by_bot and m.id in self.by_bot_ids[cid]:
                        m.by_bot = True

            stack = MessageEventsStack()
            if not self.last_messages_ids.get(cid):
                min_known = min(self.last_messages_ids.values(), default=10 ** 20)
                messages = [m for m in messages if m.id > min_known] or messages[-1:]

            self.last_messages_ids[cid] = messages[-1].id
            self.chat_node_tags[cid] = messages[-1].tag
            if messages[-1].interlocutor_id:
                self.users_ids[cid] = messages[-1].interlocutor_id
            self.by_bot_ids[cid] = [i for i in self.by_bot_ids.get(cid, [])
                                     if i > self.last_messages_ids[cid]]

            result[cid] = []
            for msg in messages:
                event = NewMessageEvent(self.__last_msg_event_tag, msg, stack)
                stack.add_events([event])
                result[cid].append(event)

        return result

    def parse_order_updates(self, obj: dict) -> list[AnyEvent]:
        """
        Парсит events из объекта ``orders_counters``.

        :param obj: элемент из ``response["objects"]`` с ``type == "orders_counters"``.
        :return: список событий заказов.
        """
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
    # Вспомогательные методы для управления состоянием
    # ══════════════════════════════════════════════════════════════════════════

    def update_last_message(self, chat_id: int, message_id: int,
                            message_text: str | None):
        """
        Обновляет кэш последнего сообщения чата.

        :param chat_id: ID чата.
        :param message_id: ID сообщения.
        :param message_text: текст или None (изображение).
        """
        self.runner_last_messages[chat_id] = [message_id, message_id, message_text]
        self.last_messages_ids[chat_id] = message_id

    def mark_as_by_bot(self, chat_id: int, message_id: int):
        """
        Помечает сообщение как отправленное ботом (через :meth:`Account.send_message`).

        :param chat_id: ID чата.
        :param message_id: ID сообщения.
        """
        if self.by_bot_ids.get(chat_id) is None:
            self.by_bot_ids[chat_id] = [message_id]
        else:
            self.by_bot_ids[chat_id].append(message_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Основной генератор событий
    # ══════════════════════════════════════════════════════════════════════════

    def listen(self, requests_delay: int | float = 6.0,
               ignore_exceptions: bool = True) -> Generator[AnyEvent, None, None]:
        """
        Бесконечно опрашивает FunPay и генерирует события.

        :param requests_delay: пауза между итерациями (секунды).
        :param ignore_exceptions: не бросать исключения (только логировать).
        :return: генератор событий.

        Пример::

            runner = Runner(account)
            for event in runner.listen():
                if isinstance(event, NewMessageEvent):
                    print(event.message.text)
        """
        while True:
            start_time = time.time()
            try:
                if not (self.__orders_counters and self.__chat_bookmarks):
                    updates_objects = self.get_updates()["objects"]
                    is_request_made = True
                else:
                    # Используем накопленные данные из loop()
                    updates_objects = [self.__orders_counters]
                    chat_ids_seen: set = set()
                    for cb in reversed(self.__chat_bookmarks):
                        cb_ids = set(cb["data"]["order"])
                        if chat_ids_seen.issuperset(cb_ids):
                            continue
                        chat_ids_seen.update(cb_ids)
                        updates_objects.append(cb)
                    is_request_made = False

                self.__orders_counters = None
                self.__chat_bookmarks = []
                events = self.parse_updates(updates_objects)

                if is_request_made and not events:
                    self.__chat_nodes = {}

                for event in events:
                    yield event

            except Exception as e:
                if not ignore_exceptions:
                    raise e
                logger.error("Ошибка при получении событий (нечастая — не критично).")
                logger.debug("TRACEBACK", exc_info=True)

            iteration_time = time.time() - start_time
            # Если были 429-ошибки недавно — не ускоряемся
            if time.time() - self.account.last_429_err_time > 60:
                rt = requests_delay - iteration_time
                if rt > 0:
                    time.sleep(rt)
            else:
                time.sleep(requests_delay)