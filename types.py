"""
В данном модуле описаны все типы пакета FunPayAPI.
"""
from __future__ import annotations

import re
from typing import Literal, overload, Optional
import datetime

from .common.utils import RegularExpressions
from .common.enums import MessageTypes, OrderStatuses, SubCategoryTypes, Currency


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные базовые классы
# ──────────────────────────────────────────────────────────────────────────────

class BaseOrderInfo:
    """
    Базовый класс с ленивой загрузкой полного объекта заказа.
    Используется в :class:`ChatShortcut`, :class:`Message`, :class:`OrderShortcut`.
    """

    def __init__(self):
        self._order: Order | None = None
        self._order_attempt_made: bool = False
        self._order_attempt_error: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Чаты
# ──────────────────────────────────────────────────────────────────────────────

class ChatShortcut(BaseOrderInfo):
    """
    Данный класс представляет виджет чата со страницы https://funpay.com/chat/

    :param id_: ID чата.
    :param name: название чата (никнейм собеседника).
    :param last_message_text: текст последнего сообщения (макс. 250 символов).
    :param node_msg_id: ID последнего сообщения в чате.
    :param user_msg_id: ID последнего прочитанного сообщения.
    :param unread: непрочитан ли чат (оранжевый индикатор).
    :param html: HTML-код виджета чата.
    :param determine_msg_type: определять ли тип последнего сообщения.
    """

    def __init__(self, id_: int, name: str, last_message_text: str,
                 node_msg_id: int, user_msg_id: int,
                 unread: bool, html: str, determine_msg_type: bool = True):
        self.id: int = id_
        self.name: str | None = name if name else None
        self.last_message_text: str = last_message_text
        self.last_by_bot: bool | None = None
        self.last_by_vertex: bool | None = None
        self.unread: bool = unread
        self.node_msg_id: int = node_msg_id
        self.user_msg_id: int = user_msg_id
        self.last_message_type: MessageTypes | None = (
            None if not determine_msg_type else self.get_last_message_type()
        )
        self.html: str = html
        BaseOrderInfo.__init__(self)

    def get_last_message_type(self) -> MessageTypes:
        """
        Определяет тип последнего сообщения в чате по регулярным выражениям.

        .. warning::
            Результат не является 100% надёжным — возможны ложные срабатывания,
            если пользователь напишет сообщение, совпадающее с одним из паттернов.

        :return: тип последнего сообщения.
        :rtype: :class:`FunPayAPI.common.enums.MessageTypes`
        """
        res = RegularExpressions()
        text = self.last_message_text

        if res.DISCORD.search(text):
            return MessageTypes.DISCORD
        if res.DEAR_VENDORS.search(text):
            return MessageTypes.DEAR_VENDORS
        if res.ORDER_PURCHASED.findall(text) and res.ORDER_PURCHASED2.findall(text):
            return MessageTypes.ORDER_PURCHASED
        if res.ORDER_ID.search(text) is None:
            return MessageTypes.NON_SYSTEM

        sys_msg_types = {
            MessageTypes.ORDER_CONFIRMED: res.ORDER_CONFIRMED,
            MessageTypes.NEW_FEEDBACK: res.NEW_FEEDBACK,
            MessageTypes.NEW_FEEDBACK_ANSWER: res.NEW_FEEDBACK_ANSWER,
            MessageTypes.FEEDBACK_CHANGED: res.FEEDBACK_CHANGED,
            MessageTypes.FEEDBACK_DELETED: res.FEEDBACK_DELETED,
            MessageTypes.REFUND: res.REFUND,
            MessageTypes.FEEDBACK_ANSWER_CHANGED: res.FEEDBACK_ANSWER_CHANGED,
            MessageTypes.FEEDBACK_ANSWER_DELETED: res.FEEDBACK_ANSWER_DELETED,
            MessageTypes.ORDER_CONFIRMED_BY_ADMIN: res.ORDER_CONFIRMED_BY_ADMIN,
            MessageTypes.PARTIAL_REFUND: res.PARTIAL_REFUND,
            MessageTypes.ORDER_REOPENED: res.ORDER_REOPENED,
            MessageTypes.REFUND_BY_ADMIN: res.REFUND_BY_ADMIN,
        }

        for msg_type, pattern in sys_msg_types.items():
            if pattern.search(text):
                return msg_type
        return MessageTypes.NON_SYSTEM

    def __str__(self):
        return self.last_message_text


class BuyerViewing:
    """
    Данный класс представляет поле «Покупатель смотрит» из ответа runner'а.

    :param buyer_id: ID покупателя.
    :param link: ссылка на лот, который он просматривает.
    :param text: текстовое описание лота.
    :param tag: тег события.
    :param html: исходный HTML-код блока просмотра.
    """

    def __init__(self, buyer_id: int, link: str | None, text: str | None,
                 tag: str | None, html: str | None = None):
        self.buyer_id: int = buyer_id
        self.link: str | None = link
        self.text: str | None = text
        self.tag: str | None = tag
        self.html: str | None = html
        self.is_viewing_lot: bool = bool(self.link)

    @property
    def lot_id(self) -> str | int | None:
        """ID просматриваемого лота (int если числовой, иначе str), или None."""
        if self.is_viewing_lot:
            id_ = self.link.split("=")[-1]
            return int(id_) if id_.isdigit() else id_
        return None

    @property
    def subcategory_type(self) -> SubCategoryTypes | None:
        """Тип подкатегории просматриваемого лота."""
        if self.is_viewing_lot:
            return SubCategoryTypes.COMMON if "/lots/" in self.link else SubCategoryTypes.CURRENCY
        return None


class Chat:
    """
    Данный класс представляет личный чат.

    :param id_: ID чата.
    :param name: никнейм собеседника.
    :param looking_link: ссылка на лот, который смотрит собеседник.
    :param looking_text: название лота, который смотрит собеседник.
    :param html: HTML-код страницы чата.
    :param messages: до 100 последних сообщений чата.
    """

    def __init__(self, id_: int, name: str,
                 looking_link: str | None, looking_text: str | None,
                 html: str, messages: Optional[list[Message]] = None):
        self.id: int = id_
        self.name: str = name
        self.looking_link: str | None = looking_link
        self.looking_text: str | None = looking_text
        self.html: str = html
        self.messages: list[Message] = messages or []


# ──────────────────────────────────────────────────────────────────────────────
# Сообщения
# ──────────────────────────────────────────────────────────────────────────────

class Message(BaseOrderInfo):
    """
    Данный класс представляет отдельное сообщение в чате.

    :param id_: ID сообщения.
    :param text: текст сообщения (None если изображение).
    :param chat_id: ID чата.
    :param chat_name: название чата (никнейм собеседника).
    :param interlocutor_id: ID собеседника.
    :param author: никнейм автора.
    :param author_id: ID автора.
    :param html: HTML-код сообщения.
    :param image_link: ссылка на изображение (если есть).
    :param image_name: имя файла изображения (если есть).
    :param determine_msg_type: определять ли тип сообщения автоматически.
    :param badge_text: текст бейджика (поддержка, модерация и т.п.).
    :param tag: тег чата на момент получения сообщения.
    """

    def __init__(self, id_: int, text: str | None, chat_id: int | str,
                 chat_name: str | None, interlocutor_id: int | None,
                 author: str | None, author_id: int, html: str,
                 image_link: str | None = None, image_name: str | None = None,
                 determine_msg_type: bool = True,
                 badge_text: Optional[str] = None,
                 tag: Optional[str] = None):
        self.id: int = id_
        self.text: str | None = text
        self.chat_id: int | str = chat_id
        self.chat_name: str | None = chat_name
        self.interlocutor_id: int | None = interlocutor_id
        self.buyer_viewing: BuyerViewing | None = None
        self.type: MessageTypes | None = None if not determine_msg_type else self.get_message_type()
        self.author: str | None = author
        self.author_id: int = author_id
        self.html: str = html
        self.image_link: str | None = image_link
        self.image_name: str | None = image_name
        self.by_bot: bool = False
        self.by_vertex: bool = False
        self.badge: str | None = badge_text
        self.is_employee: bool = False
        self.is_support: bool = False
        self.is_moderation: bool = False
        self.is_arbitration: bool = False
        self.is_autoreply: bool = False
        self.initiator_username: str | None = None
        self.initiator_id: int | None = None
        self.i_am_seller: bool | None = None
        self.i_am_buyer: bool | None = None
        self.tag: str | None = tag
        BaseOrderInfo.__init__(self)

    def get_message_type(self) -> MessageTypes:
        """
        Определяет тип сообщения по регулярным выражениям.

        .. warning::
            Не является 100% надёжным. Рекомендуется дополнительно проверять ``author_id == 0``
            для системных сообщений.

        :return: тип сообщения.
        :rtype: :class:`FunPayAPI.common.enums.MessageTypes`
        """
        if not self.text:
            return MessageTypes.NON_SYSTEM

        res = RegularExpressions()
        text = self.text

        if res.DISCORD.search(text):
            return MessageTypes.DISCORD
        if res.DEAR_VENDORS.search(text):
            return MessageTypes.DEAR_VENDORS
        if res.ORDER_PURCHASED.findall(text) and res.ORDER_PURCHASED2.findall(text):
            return MessageTypes.ORDER_PURCHASED
        if res.ORDER_ID.search(text) is None:
            return MessageTypes.NON_SYSTEM

        sys_msg_types = {
            MessageTypes.ORDER_CONFIRMED: res.ORDER_CONFIRMED,
            MessageTypes.NEW_FEEDBACK: res.NEW_FEEDBACK,
            MessageTypes.NEW_FEEDBACK_ANSWER: res.NEW_FEEDBACK_ANSWER,
            MessageTypes.FEEDBACK_CHANGED: res.FEEDBACK_CHANGED,
            MessageTypes.FEEDBACK_DELETED: res.FEEDBACK_DELETED,
            MessageTypes.REFUND: res.REFUND,
            MessageTypes.FEEDBACK_ANSWER_CHANGED: res.FEEDBACK_ANSWER_CHANGED,
            MessageTypes.FEEDBACK_ANSWER_DELETED: res.FEEDBACK_ANSWER_DELETED,
            MessageTypes.ORDER_CONFIRMED_BY_ADMIN: res.ORDER_CONFIRMED_BY_ADMIN,
            MessageTypes.PARTIAL_REFUND: res.PARTIAL_REFUND,
            MessageTypes.ORDER_REOPENED: res.ORDER_REOPENED,
            MessageTypes.REFUND_BY_ADMIN: res.REFUND_BY_ADMIN,
        }

        for msg_type, pattern in sys_msg_types.items():
            if pattern.search(text):
                return msg_type
        return MessageTypes.NON_SYSTEM

    def __str__(self):
        if self.text is not None:
            return self.text
        if self.image_link is not None:
            return self.image_link
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Заказы
# ──────────────────────────────────────────────────────────────────────────────

class Server:
    """Сервер, указанный в заказе."""

    def __init__(self, id_: int, name: str | None = None):
        self.id: int = id_
        self.name: str | None = name


class Side:
    """Сторона, указанная в заказе."""

    def __init__(self, id_: int, name: str | None = None):
        self.id: int = id_
        self.name: str | None = name


class LotField:
    """Поле лота из детальной информации о заказе."""

    def __init__(self, id: str, value: str | dict,
                 name: str | None = None, field_type_id: str | None = None):
        self.id: str = id
        self.value: str | dict = value
        self.name: str | None = name
        self.field_type_id: str | None = field_type_id


class OrderShortcut(BaseOrderInfo):
    """
    Данный класс представляет виджет заказа со страницы https://funpay.com/orders/trade

    :param id_: ID заказа (с «#» или без).
    :param description: описание заказа.
    :param price: цена заказа.
    :param currency: валюта заказа.
    :param buyer_username: никнейм покупателя.
    :param buyer_id: ID покупателя.
    :param chat_id: ID чата.
    :param status: статус заказа.
    :param date: дата создания заказа.
    :param subcategory_name: название подкатегории.
    :param subcategory: объект подкатегории (или None).
    :param html: HTML-код виджета заказа.
    :param dont_search_amount: не парсить количество товара.
    """

    def __init__(self, id_: str, description: str, price: float, currency: Currency,
                 buyer_username: str, buyer_id: int, chat_id: int | str,
                 status: OrderStatuses, date: datetime.datetime,
                 subcategory_name: str, subcategory: SubCategory | None,
                 html: str, dont_search_amount: bool = False):
        self.id: str = id_[1:] if id_.startswith("#") else id_
        self.description: str = description
        self.price: float = price
        self.currency: Currency = currency
        self.amount: int | None = self.parse_amount() if not dont_search_amount else None
        self.buyer_username: str = buyer_username
        self.buyer_id: int = buyer_id
        self.chat_id: int | str = chat_id
        self.status: OrderStatuses = status
        self.date: datetime.datetime = date
        self.subcategory_name: str = subcategory_name
        self.subcategory: SubCategory | None = subcategory
        self.html: str = html
        BaseOrderInfo.__init__(self)

    def parse_amount(self) -> int:
        """
        Парсит количество купленного товара из описания заказа.

        :return: количество товара (1 если не найдено).
        :rtype: :obj:`int`
        """
        res = RegularExpressions()
        result = res.PRODUCTS_AMOUNT.findall(self.description)
        if result:
            return int(result[0][0].replace(" ", ""))
        return 1

    def __str__(self):
        return self.description


class Order:
    """
    Данный класс представляет полную информацию о заказе
    (получается через API ``/api/orders/get``).

    :param id_: ID заказа.
    :param status: статус заказа.
    :param subcategory: подкатегория заказа.
    :param server: сервер.
    :param side: сторона.
    :param fields: поля оплаченного лота.
    :param amount: количество товара.
    :param sum_: сумма заказа.
    :param currency: валюта.
    :param player: имя персонажа.
    :param buyer_id: ID покупателя.
    :param buyer_username: никнейм покупателя.
    :param seller_id: ID продавца.
    :param seller_username: никнейм продавца.
    :param chat_id: ID чата.
    :param review: отзыв на заказ.
    :param order_secrets: товары встроенной автовыдачи FunPay.
    :param locale: локаль заказа.
    """

    def __init__(self, id_: str, status: OrderStatuses,
                 subcategory: SubCategory | None,
                 server: Server | None, side: Side | None,
                 fields: dict[str, LotField], amount: int,
                 sum_: float, currency: Currency, player: str | None,
                 buyer_id: int, buyer_username: str | None,
                 seller_id: int, seller_username: str | None,
                 chat_id: str | int,
                 review: Review | None, order_secrets: list[str],
                 locale: Literal["ru", "en", "uk"]):
        self.id: str = id_[1:] if id_.startswith("#") else id_
        self.status: OrderStatuses = status
        self.subcategory: SubCategory | None = subcategory
        self.fields: dict[str, LotField] = fields
        self.sum: float = sum_
        self.currency: Currency = currency
        self.buyer_id: int = buyer_id
        self.buyer_username: str | None = buyer_username
        self.seller_id: int = seller_id
        self.seller_username: str | None = seller_username
        self.chat_id: str | int = chat_id
        self.review: Review | None = review
        self.amount: int = amount
        self.locale: Literal["ru", "en"] = "en" if locale == "en" else "ru"
        self.player: str | None = player
        self.server: Server | None = server
        self.side: Side | None = side
        self.order_secrets: list[str] = order_secrets

    # ── Удобные методы доступа к полям ──────────────────────────────────────

    def get_field(self, key: str) -> LotField | None:
        """Возвращает поле лота по ключу."""
        return self.fields.get(key)

    def get_field_value(self, key: str, locale: Literal["ru", "en"] = "ru") -> str | None:
        """Возвращает значение поля по ключу и локали."""
        field = self.get_field(key)
        if not field:
            return None
        if isinstance(field.value, dict):
            return field.value.get(locale)
        return field.value

    def get_field_value_any(self, key: str) -> str | None:
        """Возвращает значение поля, используя приоритет локали заказа."""
        locales = [self.locale] + [l for l in ("ru", "en") if l != self.locale]
        for loc in locales:
            value = self.get_field_value(key, loc)
            if value:
                return value
        return None

    @property
    def short_description(self) -> str | None:
        return self.get_field_value_any("summary")

    @property
    def title(self) -> str | None:
        return self.short_description

    @property
    def full_description(self) -> str | None:
        return self.get_field_value_any("desc")

    @property
    def payment_msg(self) -> str | None:
        return self.get_field_value_any("payment_msg")

    @property
    def lot_params(self) -> list[tuple[str, str]]:
        """Параметры лота (кроме описания и сообщения покупателю) в виде списка кортежей."""
        result = []
        for key, field in self.fields.items():
            if key in ("payment_msg", "desc", "summary"):
                continue
            v = self.get_field_value_any(key)
            result.append((field.name, v))
        return result

    @property
    def lot_params_text(self) -> str | None:
        """Параметры лота в виде строки, разделённой запятыми."""
        result = None
        for key, field in self.fields.items():
            if key in ("payment_msg", "desc", "summary"):
                continue
            v = self.get_field_value_any(key)
            if not v:
                continue
            s = f"{v} {field.name}" if (isinstance(v, int) or str(v).isdigit()) else v
            result = f"{result}, {s}" if result else s
        return result

    @property
    def lot_params_dict(self) -> dict[str, str]:
        """Параметры лота в виде словаря ``{название: значение}``.

        .. warning::
            Если названия полей дублируются — часть данных будет утеряна.
        """
        d = {}
        for key, field in self.fields.items():
            if key in ("payment_msg", "desc", "summary"):
                continue
            d[field.name] = self.get_field_value_any(key)
        return d

    @property
    def character_name(self) -> str | None:
        """Имя персонажа."""
        return self.player

    def __str__(self):
        return f"#{self.id}"


# ──────────────────────────────────────────────────────────────────────────────
# Категории и подкатегории
# ──────────────────────────────────────────────────────────────────────────────

class Category:
    """
    Класс, описывающий категорию (игру) FunPay.

    :param id_: ID категории (data-id).
    :param name: название игры.
    :param subcategories: подкатегории (опционально).
    :param position: порядковый номер в списке (для сортировки).
    """

    def __init__(self, id_: int, name: str,
                 subcategories: list[SubCategory] | None = None,
                 position: int = 100_000):
        self.id: int = id_
        self.name: str = name
        self.position: int = position
        self.__subcategories: list[SubCategory] = subcategories or []
        self.__sorted_subcategories: dict[SubCategoryTypes, dict[int, SubCategory]] = {
            SubCategoryTypes.COMMON: {},
            SubCategoryTypes.CURRENCY: {},
        }
        for sc in self.__subcategories:
            self.__sorted_subcategories[sc.type][sc.id] = sc

    def add_subcategory(self, subcategory: SubCategory):
        """Добавляет подкатегорию."""
        if subcategory not in self.__subcategories:
            self.__subcategories.append(subcategory)
            self.__sorted_subcategories[subcategory.type][subcategory.id] = subcategory

    def get_subcategory(self, subcategory_type: SubCategoryTypes,
                        subcategory_id: int) -> SubCategory | None:
        """Возвращает подкатегорию по типу и ID."""
        return self.__sorted_subcategories[subcategory_type].get(subcategory_id)

    def get_subcategories(self) -> list[SubCategory]:
        """Возвращает все подкатегории данной категории."""
        return self.__subcategories

    def get_sorted_subcategories(self) -> dict[SubCategoryTypes, dict[int, SubCategory]]:
        """Возвращает подкатегории в виде словаря ``{тип: {ID: подкатегория}}``."""
        return self.__sorted_subcategories


class SubCategory:
    """
    Класс, описывающий подкатегорию FunPay.

    :param id_: ID подкатегории.
    :param name: название подкатегории.
    :param type_: тип лотов.
    :param category: родительская категория.
    :param position: порядковый номер для сортировки.
    """

    def __init__(self, id_: int, name: str, type_: SubCategoryTypes,
                 category: Category, position: int = 100_000):
        self.id: int = id_
        self.name: str = name
        self.type: SubCategoryTypes = type_
        self.category: Category = category
        self.position: int = position
        self.fullname: str = f"{self.name} {self.category.name}"
        self.public_link: str = (
            f"https://funpay.com/chips/{id_}/"
            if type_ is SubCategoryTypes.CURRENCY
            else f"https://funpay.com/lots/{id_}/"
        )
        self.private_link: str = f"{self.public_link}trade"

    @property
    def is_common(self) -> bool:
        return self.type == SubCategoryTypes.COMMON

    @property
    def is_lots(self) -> bool:
        return self.is_common

    @property
    def is_currency(self) -> bool:
        return self.type == SubCategoryTypes.CURRENCY

    @property
    def is_chips(self) -> bool:
        return self.is_currency

    @property
    def ui_name(self) -> str:
        return f"{self.category.name} / {self.name}"

    def telegram_text(self, link: Literal["private", "public", None] = None) -> str:
        """Возвращает HTML-ссылку для Telegram или просто название."""
        if link == "private":
            return f"<a href='{self.private_link}'>{self.ui_name}</a>"
        if link == "public":
            return f"<a href='{self.public_link}'>{self.ui_name}</a>"
        return self.ui_name


# ──────────────────────────────────────────────────────────────────────────────
# Поля лота и управление лотами
# ──────────────────────────────────────────────────────────────────────────────

class LotFields:
    """
    Класс, описывающий редактируемые поля лота.

    :param lot_id: ID лота.
    :param fields: словарь с raw-полями формы.
    :param subcategory: подкатегория лота.
    :param currency: валюта лота.
    :param calc_result: объект рассчитанной комиссии.
    :param db_amount: количество в БД FunPay (для определения активности).
    """

    def __init__(self, lot_id: int, fields: dict,
                 subcategory: SubCategory | None = None,
                 currency: Currency = Currency.UNKNOWN,
                 calc_result: CalcResult | None = None,
                 db_amount: int | None = None):
        self.lot_id: int = lot_id
        self.__fields: dict = fields

        self.title_ru: str = self.__fields.get("fields[summary][ru]", "")
        self.title_en: str = self.__fields.get("fields[summary][en]", "")
        self.description_ru: str = self.__fields.get("fields[desc][ru]", "")
        self.description_en: str = self.__fields.get("fields[desc][en]", "")
        self.payment_msg_ru: str = self.__fields.get("fields[payment_msg][ru]", "")
        self.payment_msg_en: str = self.__fields.get("fields[payment_msg][en]", "")
        self.images: list[int] = [
            int(i) for i in self.__fields.get("fields[images]", "").split(",") if i
        ]
        self.auto_delivery: bool | None = (
            bool(self.__fields["auto_delivery"]) if "auto_delivery" in self.__fields else None
        )
        self.secrets: list[str] = [
            i for i in self.__fields.get("secrets", "").strip().split("\n") if i
        ]
        _amount_raw = self.__fields.get("amount")
        self._amount: int | None = (
            int(_amount_raw) if _amount_raw is not None and bool(_amount_raw) else
            (0 if _amount_raw is not None else None)
        )
        self.price: float | None = (
            float(i) if (i := self.__fields.get("price")) else None
        )
        self.active: bool = (
            False if db_amount == 0 else self.__fields.get("active") == "on"
        )
        self.deactivate_after_sale: bool | None = (
            bool(self.__fields["deactivate_after_sale"])
            if "deactivate_after_sale" in self.__fields else None
        )
        self.subcategory: SubCategory | None = subcategory
        self.currency: Currency = currency
        self.csrf_token: str | None = self.__fields.get("csrf_token")
        self.calc_result: CalcResult | None = calc_result

    @property
    def amount(self) -> int | None:
        """
        Количество товара.
        Если включена автовыдача FunPay — возвращает количество товаров в ней.
        """
        if self.auto_delivery:
            return len(self.secrets)
        return self._amount

    @amount.setter
    def amount(self, value: int | None):
        self._amount = value

    @property
    def public_link(self) -> str:
        return f"https://funpay.com/lots/offer?id={self.lot_id}"

    @property
    def private_link(self) -> str:
        return f"https://funpay.com/lots/offerEdit?offer={self.lot_id}"

    @property
    def fields(self) -> dict[str, str]:
        """Возвращает все raw-поля лота."""
        return self.__fields

    def edit_fields(self, fields: dict[str, str]):
        """Обновляет переданные поля."""
        self.__fields.update(fields)

    def set_fields(self, fields: dict):
        """
        Полностью заменяет поля.

        .. warning::
            Не редактирует свойства экземпляра — только внутренний словарь.
        """
        self.__fields = fields

    def renew_fields(self) -> LotFields:
        """
        Синхронизирует внутренний словарь полей со свойствами экземпляра.
        Нужно вызывать перед сохранением лота на FunPay.

        :return: self (для цепочки вызовов).
        """
        self.__fields["offer_id"] = str(self.lot_id or 0)
        self.__fields["fields[summary][ru]"] = self.title_ru
        self.__fields["fields[summary][en]"] = self.title_en
        self.__fields["fields[desc][ru]"] = self.description_ru
        self.__fields["fields[desc][en]"] = self.description_en
        self.__fields["fields[payment_msg][ru]"] = self.payment_msg_ru
        self.__fields["fields[payment_msg][en]"] = self.payment_msg_en
        self.__fields["price"] = str(self.price) if self.price is not None else ""
        self.__fields["active"] = "on" if self.active else ""
        self.__fields["fields[images]"] = ",".join(map(str, self.images))
        self.__fields["secrets"] = "\n".join(self.secrets)
        self.__fields["csrf_token"] = self.csrf_token

        if self._amount is not None:
            self.__fields["amount"] = self._amount or ""
        else:
            self.__fields.pop("amount", None)

        if self.deactivate_after_sale is not None:
            self.__fields["deactivate_after_sale"] = (
                "on" if self.deactivate_after_sale else ""
            )
        else:
            self.__fields.pop("deactivate_after_sale", None)

        if self.auto_delivery is not None:
            self.__fields["auto_delivery"] = "on" if self.auto_delivery else ""
        else:
            self.__fields.pop("auto_delivery", None)

        return self


class ChipOffer:
    """Предложение валюты в подкатегории chips."""

    def __init__(self, lot_id: str, active: bool = False,
                 server: str | None = None, side: str | None = None,
                 price: float | None = None, amount: int | None = None):
        self.lot_id: str = lot_id
        self.active: bool = active
        self.server: str | None = server
        self.side: str | None = side
        self.price: float | None = price
        self.amount: int | None = amount

    @property
    def key(self) -> str:
        s = "".join([f"[{i}]" for i in self.lot_id.split("-")[3:]])
        return f"offers{s}"


class ChipFields:
    """Редактируемые поля лота-валюты."""

    def __init__(self, account_id: int, subcategory_id: int, fields: dict[str, str]):
        self.subcategory_id: int = subcategory_id
        self.__fields: dict = fields
        self.min_sum: float | None = (
            float(i) if (i := self.__fields.get("options[chip_min_sum]")) else None
        )
        self.account_id: int = account_id
        self.game_id: int = int(self.__fields.get("game"))
        self.csrf_token: str | None = self.__fields.get("csrf_token")
        self.chip_offers: dict[str, ChipOffer] = {}
        self.__parse_offers()

    @property
    def fields(self) -> dict[str, str]:
        return self.__fields

    def renew_fields(self) -> ChipFields:
        """Синхронизирует внутренний словарь со свойствами. Вызывать перед сохранением."""
        self.__fields["game"] = str(self.game_id)
        self.__fields["chip"] = str(self.subcategory_id)
        self.__fields["options[chip_min_sum]"] = str(self.min_sum) if self.min_sum is not None else ""
        self.__fields["csrf_token"] = self.csrf_token
        for chip_offer in self.chip_offers.values():
            key = chip_offer.key
            self.__fields[f"{key}[amount]"] = str(chip_offer.amount) if chip_offer.amount is not None else ""
            self.__fields[f"{key}[price]"] = str(chip_offer.price) if chip_offer.price is not None else ""
            if chip_offer.active:
                self.__fields[f"{key}[active]"] = "on"
            else:
                self.__fields.pop(f"{key}[active]", None)
        return self

    def __parse_offers(self):
        for k, v in self.__fields.items():
            if not k.startswith("offers"):
                continue
            nums = re.findall(r'\d+', k)
            key = "-".join(map(str, nums))
            offer_id = f"{self.account_id}-{self.game_id}-{self.subcategory_id}-{key}"
            if offer_id not in self.chip_offers:
                self.chip_offers[offer_id] = ChipOffer(offer_id)
            chip_offer = self.chip_offers[offer_id]
            field = k.split("[")[-1].rstrip("]")
            if field == "active":
                chip_offer.active = v == "on"
            elif field == "price":
                chip_offer.price = float(v) if v else None
            elif field == "amount":
                chip_offer.amount = int(v) if v else None


# ──────────────────────────────────────────────────────────────────────────────
# Страница лота
# ──────────────────────────────────────────────────────────────────────────────

class LotPage:
    """
    Класс, описывающий публичную страницу лота (``/lots/offer?id=...``).

    :param lot_id: ID лота.
    :param subcategory: подкатегория лота.
    :param short_description: краткое описание.
    :param full_description: подробное описание.
    :param image_urls: список URL изображений.
    :param seller_id: ID продавца.
    :param seller_username: никнейм продавца.
    """

    def __init__(self, lot_id: int, subcategory: SubCategory | None,
                 short_description: str | None, full_description: str | None,
                 image_urls: list[str], seller_id: int, seller_username: str):
        self.lot_id: int = lot_id
        self.subcategory: SubCategory | None = subcategory
        self.short_description: str | None = short_description
        self.full_description: str | None = full_description
        self.image_urls: list[str] = image_urls
        self.seller_id: int = seller_id
        self.seller_username: str = seller_username

    @property
    def seller_url(self) -> str:
        return f"https://funpay.com/users/{self.seller_id}/"


# ──────────────────────────────────────────────────────────────────────────────
# Лоты (публичные и собственные)
# ──────────────────────────────────────────────────────────────────────────────

class SellerShortcut:
    """Краткая информация о продавце из таблицы предложений."""

    def __init__(self, id_: int, username: str, online: bool,
                 stars: int | None, reviews: int, html: str):
        self.id: int = id_
        self.username: str = username
        self.online: bool = online
        self.stars: int | None = stars
        self.reviews: int = reviews
        self.html: str = html

    @property
    def link(self) -> str:
        return f"https://funpay.com/users/{self.id}/"


class LotShortcut:
    """
    Данный класс представляет виджет лота из публичного списка
    (страница подкатегории или профиль пользователя).

    :param id_: ID лота.
    :param server: название сервера (если указан).
    :param side: сторона (если указана).
    :param description: краткое описание лота.
    :param amount: количество товара.
    :param price: цена лота.
    :param currency: валюта.
    :param subcategory: подкатегория лота.
    :param seller: объект продавца (только для лотов из таблицы).
    :param auto: включена ли автовыдача FunPay.
    :param promo: находится ли лот в закрепе.
    :param attributes: атрибуты data-* из HTML.
    :param html: HTML-код виджета.
    """

    def __init__(self, id_: int | str, server: str | None, side: str | None,
                 description: str | None, amount: int | None, price: float,
                 currency: Currency, subcategory: SubCategory | None,
                 seller: SellerShortcut | None, auto: bool, promo: bool | None,
                 attributes: dict[str, int | str] | None, html: str):
        self.id: int | str = int(id_) if isinstance(id_, str) and id_.isnumeric() else id_
        self.server: str | None = server
        self.side: str | None = side
        self.description: str | None = description
        self.title: str | None = description
        self.amount: int | None = amount
        self.price: float = price
        self.currency: Currency = currency
        self.seller: SellerShortcut | None = seller
        self.auto: bool = auto
        self.promo: bool | None = promo
        self.attributes: dict[str, int | str] | None = attributes
        self.subcategory: SubCategory | None = subcategory
        self.html: str = html
        self.public_link: str = (
            f"https://funpay.com/chips/offer?id={self.id}"
            if subcategory and subcategory.type is SubCategoryTypes.CURRENCY
            else f"https://funpay.com/lots/offer?id={self.id}"
        )


class MyLotShortcut:
    """
    Данный класс представляет виджет лота со страницы ``/lots/{id}/trade``
    (собственные лоты на аккаунте).

    :param id_: ID лота.
    :param server: сервер (если указан).
    :param side: сторона (если указана).
    :param description: краткое описание.
    :param amount: количество товара.
    :param price: цена.
    :param currency: валюта.
    :param subcategory: подкатегория.
    :param auto: включена ли автовыдача FunPay.
    :param active: активен ли лот.
    :param html: HTML-код виджета.
    """

    def __init__(self, id_: int | str, server: str | None, side: str | None,
                 description: str | None, amount: int | None, price: float,
                 currency: Currency, subcategory: SubCategory | None,
                 auto: bool, active: bool, html: str):
        self.id: int | str = int(id_) if isinstance(id_, str) and id_.isnumeric() else id_
        self.server: str | None = server
        self.side: str | None = side
        self.description: str | None = description
        self.title: str | None = description
        self.amount: int | None = amount
        self.price: float = price
        self.currency: Currency = currency
        self.auto: bool = auto
        self.subcategory: SubCategory | None = subcategory
        self.active: bool = active
        self.html: str = html
        self.public_link: str = (
            f"https://funpay.com/chips/offer?id={self.id}"
            if subcategory and subcategory.type is SubCategoryTypes.CURRENCY
            else f"https://funpay.com/lots/offer?id={self.id}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Пользователи
# ──────────────────────────────────────────────────────────────────────────────

class UserProfile:
    """
    Данный класс представляет профиль пользователя FunPay.

    :param id_: ID пользователя.
    :param username: никнейм.
    :param profile_photo: ссылка на фото профиля.
    :param online: онлайн ли пользователь.
    :param banned: заблокирован ли пользователь.
    :param html: HTML-код страницы профиля.
    """

    def __init__(self, id_: int, username: str, profile_photo: str,
                 online: bool, banned: bool, html: str):
        self.id: int = id_
        self.username: str = username
        self.profile_photo: str = profile_photo
        self.online: bool = online
        self.banned: bool = banned
        self.html: str = html
        self.__lots_ids: dict[int | str, LotShortcut] = {}
        self.__sorted_by_subcategory_lots: dict[SubCategory, dict[int | str, LotShortcut]] = {}
        self.__sorted_by_subcategory_type_lots: dict[SubCategoryTypes, dict[int | str, LotShortcut]] = {
            SubCategoryTypes.COMMON: {},
            SubCategoryTypes.CURRENCY: {},
        }

    def get_lot(self, lot_id: int | str) -> LotShortcut | None:
        if isinstance(lot_id, str) and lot_id.isnumeric():
            return self.__lots_ids.get(int(lot_id))
        return self.__lots_ids.get(lot_id)

    def get_lots(self) -> list[LotShortcut]:
        return list(self.__lots_ids.values())

    @overload
    def get_sorted_lots(self, mode: Literal[1]) -> dict[int | str, LotShortcut]: ...
    @overload
    def get_sorted_lots(self, mode: Literal[2]) -> dict[SubCategory, dict[int | str, LotShortcut]]: ...
    @overload
    def get_sorted_lots(self, mode: Literal[3]) -> dict[SubCategoryTypes, dict[int | str, LotShortcut]]: ...

    def get_sorted_lots(self, mode: Literal[1, 2, 3]):
        """
        Возвращает лоты в виде словаря.

        :param mode: 1 — {ID: лот}, 2 — {подкатегория: {ID: лот}}, 3 — {тип: {ID: лот}}
        """
        if mode == 1:
            return self.__lots_ids
        elif mode == 2:
            return self.__sorted_by_subcategory_lots
        return self.__sorted_by_subcategory_type_lots

    def update_lot(self, lot: LotShortcut):
        """Обновляет лот во всех внутренних индексах."""
        self.__lots_ids[lot.id] = lot
        if lot.subcategory not in self.__sorted_by_subcategory_lots:
            self.__sorted_by_subcategory_lots[lot.subcategory] = {}
        self.__sorted_by_subcategory_lots[lot.subcategory][lot.id] = lot
        if lot.subcategory:
            self.__sorted_by_subcategory_type_lots[lot.subcategory.type][lot.id] = lot

    def add_lot(self, lot: LotShortcut):
        """Добавляет лот (игнорирует, если уже существует)."""
        if lot.id not in self.__lots_ids:
            self.update_lot(lot)

    def get_common_lots(self) -> list[LotShortcut]:
        return list(self.__sorted_by_subcategory_type_lots[SubCategoryTypes.COMMON].values())

    def get_currency_lots(self) -> list[LotShortcut]:
        return list(self.__sorted_by_subcategory_type_lots[SubCategoryTypes.CURRENCY].values())

    def __str__(self):
        return self.username


# ──────────────────────────────────────────────────────────────────────────────
# Отзывы и баланс
# ──────────────────────────────────────────────────────────────────────────────

class Review:
    """
    Данный класс представляет отзыв на заказ.

    :param stars: кол-во звёзд (1–5 или None).
    :param text: текст отзыва.
    :param reply: ответ продавца.
    :param anonymous: анонимный ли отзыв.
    :param html: HTML-код блока отзыва.
    :param hidden: скрыт ли отзыв.
    :param order_id: ID заказа.
    :param author: автор отзыва.
    :param author_id: ID автора.
    :param by_bot: оставлен ли отзыв ботом.
    :param reply_by_bot: оставлен ли ответ ботом.
    """

    def __init__(self, stars: int | None, text: str | None, reply: str | None,
                 anonymous: bool, html: str, hidden: bool,
                 order_id: str | None = None, author: str | None = None,
                 author_id: int | None = None, by_bot: bool = False,
                 reply_by_bot: bool = False):
        self.stars: int | None = stars
        self.text: str | None = text
        self.reply: str | None = reply
        self.anonymous: bool = anonymous
        self.html: str = html
        self.hidden: bool = hidden
        self.order_id: str | None = (
            order_id[1:] if order_id and order_id.startswith("#") else order_id
        )
        self.author: str | None = author
        self.author_id: int | None = author_id
        self.by_bot: bool = by_bot
        self.reply_by_bot: bool = reply_by_bot


class Balance:
    """
    Информация о балансе аккаунта со страницы вывода средств.

    :param total_rub: общий рублёвый баланс.
    :param available_rub: доступный к выводу рублёвый баланс.
    :param total_usd: общий долларовый баланс.
    :param available_usd: доступный к выводу долларовый баланс.
    :param total_eur: общий евро баланс.
    :param available_eur: доступный к выводу евро баланс.
    """

    def __init__(self, total_rub: float, available_rub: float,
                 total_usd: float, available_usd: float,
                 total_eur: float, available_eur: float):
        self.total_rub: float = total_rub
        self.available_rub: float = available_rub
        self.total_usd: float = total_usd
        self.available_usd: float = available_usd
        self.total_eur: float = total_eur
        self.available_eur: float = available_eur


class PaymentMethod:
    """Платёжный метод при расчёте цены для покупателя."""

    def __init__(self, name: str | None, price: float,
                 currency: Currency, position: int | None):
        self.name: str | None = name
        self.price: float = price
        self.currency: Currency = currency
        self.position: int | None = position


class CalcResult:
    """
    Результат запроса на расчёт комиссии подкатегории.

    :param subcategory_type: тип подкатегории.
    :param subcategory_id: ID подкатегории.
    :param methods: список доступных платёжных методов.
    :param price: цена без комиссии.
    :param min_price_with_commission: минимальная цена с комиссией из ответа FunPay.
    :param min_price_currency: валюта минимальной цены.
    :param account_currency: валюта аккаунта.
    """

    def __init__(self, subcategory_type: SubCategoryTypes, subcategory_id: int,
                 methods: list[PaymentMethod], price: float,
                 min_price_with_commission: float | None,
                 min_price_currency: Currency, account_currency: Currency):
        self.subcategory_type: SubCategoryTypes = subcategory_type
        self.subcategory_id: int = subcategory_id
        self.methods: list[PaymentMethod] = methods
        self.price: float = price
        self.min_price_with_commission: float | None = min_price_with_commission
        self.min_price_currency: Currency = min_price_currency
        self.account_currency: Currency = account_currency

    def get_coefficient(self, currency: Currency) -> float:
        """Отношение цены с комиссией (в указанной валюте) к цене без комиссии."""
        if (self.min_price_with_commission and
                currency == self.min_price_currency == self.account_currency):
            return self.min_price_with_commission / self.price
        res = min(
            filter(lambda x: x.currency == currency, self.methods),
            key=lambda x: x.price, default=None
        )
        if not res:
            raise ValueError("Невозможно определить коэффициент комиссии.")
        return res.price / self.price

    @property
    def commission_coefficient(self) -> float:
        """Коэффициент комиссии в валюте аккаунта."""
        return self.get_coefficient(self.account_currency)

    @property
    def commission_percent(self) -> float:
        """Процент комиссии."""
        return (self.commission_coefficient - 1) * 100


# ──────────────────────────────────────────────────────────────────────────────
# Кошельки
# ──────────────────────────────────────────────────────────────────────────────

class Wallet:
    """Кошелёк со страницы https://funpay.com/account/wallets"""

    def __init__(self, type_id: str, data: str,
                 data_n: int | None = None, detail_id: int | None = None,
                 is_masked: bool = False, type_text: str | None = None):
        self.detail_id: int | None = detail_id
        self.type_id: str = type_id
        self.data: str = data
        self.is_masked: bool = is_masked
        self.type_text: str | None = type_text
        self.data_n: int | None = data_n